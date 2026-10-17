import Badge from "@cloudscape-design/components/badge";
import ToggleButton from "@cloudscape-design/components/toggle-button";
import Hls, { Events } from "hls.js";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import styles from "./HlsPlayer.module.css";
import { describeError, superviseSession } from "./retry";
import {
  attachPlaybackTelemetry,
  attachSgaiTelemetry,
  attachSsaiTelemetry,
  showCaptions,
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
 * Mount it only while the channel is on air (players/gate.ts); a session that fails anyway, as a starting channel's
 * does, is reopened with backoff and each reload is logged. hls.js runs its transmuxer in a blob: worker and plays
 * through MSE blob: URLs, which the console CSP allows. Captions (the channel's IMSC1 tracks) are on by default and
 * follow the CC toggle across reloads.
 */
export function HlsPlayer({ src, mode, cdnBaseUrl, label, onEvent }: HlsPlayerProps) {
  const video = useRef<HTMLVideoElement>(null);
  const session = useRef<Hls | null>(null);
  const [adState, setAdState] = useState<AdState>({ kind: "content" });
  const [captions, setCaptions] = useState(true);
  const [captioned, setCaptioned] = useState(false);
  const emit = useEffectEvent((level: PlayerEventLevel, name: string, detail: string) => {
    onEvent({ mode, level, name, detail });
  });
  const applyCaptions = useEffectEvent((hls: Hls) => {
    showCaptions(hls, captions);
  });

  useEffect(() => {
    if (session.current) {
      showCaptions(session.current, captions);
    }
  }, [captions]);

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
        enableInterstitialPlayback: mode === "sgai",
        interstitialLiveLookAhead: 10,
        // Desktop/TV start estimate so HEVC and AV1 tiers stay eligible at startup (ladders.md §3.5, §7).
        abrEwmaDefaultEstimate: 2_000_000,
      });
      attachPlaybackTelemetry(hls, sink);
      hls.on(Events.SUBTITLE_TRACKS_UPDATED, (_event, { subtitleTracks }) => {
        setCaptioned(subtitleTracks.length > 0);
        applyCaptions(hls);
      });
      session.current = hls;
      if (mode === "ssai") {
        attachSsaiTelemetry(hls, sink, cdnBaseUrl);
      } else {
        attachSgaiTelemetry(hls, sink, media);
      }
      hls.loadSource(src);
      hls.attachMedia(media);
      return hls;
    };
    const close = superviseSession(open, ({ attempt, delayMs, error }) => {
      sink.adState({ kind: "content" });
      sink.event("warning", `Retry ${attempt}`, `${describeError(error)}: reloading in ${delayMs / 1000} s`);
    });
    return () => {
      session.current = null;
      setCaptioned(false);
      close();
    };
  }, [src, mode, cdnBaseUrl]);

  return (
    <div className={styles.player}>
      <video ref={video} className={styles.video} controls muted autoPlay playsInline aria-label={label} />
      <div className={styles.badge}>
        <AdBadge state={adState} />
      </div>
      {captioned && (
        <div className={styles.captions}>
          <ToggleButton
            pressed={captions}
            onChange={({ detail }) => {
              setCaptions(detail.pressed);
            }}
            ariaLabel={`Captions for ${label}`}
          >
            CC
          </ToggleButton>
        </div>
      )}
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
