import Badge from "@cloudscape-design/components/badge";
import Hls from "hls.js";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import styles from "./HlsPlayer.module.css";
import { describeError, superviseSession } from "./retry";
import {
  attachPlaybackTelemetry,
  attachSsaiTelemetry,
  type AdState,
  type PlayerEvent,
  type PlayerEventLevel,
  type TelemetrySink,
} from "./telemetry";

export interface HlsPlayerProps {
  /** The SSAI session URL of a channel output. */
  readonly src: string;
  /** Root of the media CDN; stitched ad segments are recognised by their path below it. */
  readonly cdnBaseUrl: string;
  readonly label: string;
  readonly onEvent: (event: PlayerEvent) => void;
}

/**
 * An hls.js 1.7 player of an SSAI stream that reports its stitched ads. Give it a `key` per stream: ad state belongs
 * to one session. Mount it only while the channel is on air (players/gate.ts); a session that fails anyway, as a
 * starting channel's does, is reopened with backoff and each reload is logged. hls.js runs its transmuxer in a blob:
 * worker and plays through MSE blob: URLs, which the console CSP allows.
 */
export function HlsPlayer({ src, cdnBaseUrl, label, onEvent }: HlsPlayerProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [adState, setAdState] = useState<AdState>({ kind: "content" });
  const emit = useEffectEvent((level: PlayerEventLevel, name: string, detail: string) => {
    onEvent({ level, name, detail });
  });

  useEffect(() => {
    const media = video.current;
    if (!media) {
      return;
    }
    const sink: TelemetrySink = {
      event: (level, name, detail) => {
        emit(level, name, detail);
      },
      adState: setAdState,
    };
    if (!Hls.isSupported()) {
      media.src = src;
      sink.event("info", "Native playback", "No Media Source Extensions: ad events cannot be observed");
      return () => {
        media.removeAttribute("src");
        media.load();
      };
    }
    const open = () => {
      const hls = new Hls({
        // The playback configuration is STITCHED_ONLY: ads are segments in the playlist, never HLS Interstitials.
        enableInterstitialPlayback: false,
        // Desktop/TV start estimate: with its 500 kbit/s default, hls.js would start every session on the lowest rung.
        abrEwmaDefaultEstimate: 2_000_000,
      });
      attachPlaybackTelemetry(hls, sink);
      attachSsaiTelemetry(hls, sink, cdnBaseUrl);
      hls.loadSource(src);
      hls.attachMedia(media);
      return hls;
    };
    return superviseSession(open, ({ attempt, delayMs, error }) => {
      sink.adState({ kind: "content" });
      sink.event("warning", `Retry ${attempt}`, `${describeError(error)}: reloading in ${delayMs / 1000} s`);
    });
  }, [src, cdnBaseUrl]);

  return (
    <div className={styles.player}>
      <video ref={video} className={styles.video} controls muted autoPlay playsInline aria-label={label} />
      <div className={styles.badge}>
        <AdBadge state={adState} />
      </div>
    </div>
  );
}

function AdBadge({ state }: { state: AdState }) {
  switch (state.kind) {
    case "content":
      return <Badge color="grey">Content</Badge>;
    case "stitched":
      return <Badge color="red">Ad · stitched</Badge>;
  }
}
