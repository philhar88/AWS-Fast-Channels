import Badge from "@cloudscape-design/components/badge";
import Hls from "hls.js";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import styles from "./HlsPlayer.module.css";
import {
  attachPlaybackTelemetry,
  attachSgaiTelemetry,
  attachSsaiTelemetry,
  type AdState,
  type InsertionMode,
  type PlayerEvent,
  type PlayerEventLevel,
  type TelemetrySink,
} from "./telemetry";

export interface HlsPlayerProps {
  readonly src: string;
  readonly mode: InsertionMode;
  /** Root of the media CDN; stitched ad segments are recognised by their path below it. */
  readonly cdnBaseUrl: string;
  readonly label: string;
  readonly onEvent: (event: PlayerEvent) => void;
}

/**
 * An hls.js 1.7 player that reports ad activity. Give it a `key` per stream: ad state belongs to one session.
 * hls.js runs its transmuxer in a blob: worker and plays through MSE blob: URLs, which the console CSP allows.
 */
export function HlsPlayer({ src, mode, cdnBaseUrl, label, onEvent }: HlsPlayerProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [adState, setAdState] = useState<AdState>({ kind: "content" });
  const emit = useEffectEvent((level: PlayerEventLevel, name: string, detail: string) => {
    onEvent({ mode, level, name, detail });
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
    const hls = new Hls({
      enableInterstitialPlayback: mode === "sgai",
      interstitialLiveLookAhead: 10,
      // Desktop/TV start estimate so HEVC and AV1 tiers stay eligible at startup (ladders.md §3.5, §7).
      abrEwmaDefaultEstimate: 2_000_000,
    });
    attachPlaybackTelemetry(hls, sink);
    if (mode === "ssai") {
      attachSsaiTelemetry(hls, sink, cdnBaseUrl);
    } else {
      attachSgaiTelemetry(hls, sink, media);
    }
    hls.loadSource(src);
    hls.attachMedia(media);
    return () => {
      hls.destroy();
    };
  }, [src, mode, cdnBaseUrl]);

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
    case "guided":
      return (
        <Badge color="blue">
          Ad {state.asset} of {state.of} · guided
        </Badge>
      );
  }
}
