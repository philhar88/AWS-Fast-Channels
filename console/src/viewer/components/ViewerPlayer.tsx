import Badge from "@cloudscape-design/components/badge";
import Hls, { Events } from "hls.js";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import styles from "../../players/HlsPlayer.module.css";
import { attachBreakTelemetry, type BreakPhase, type PlayedBreak } from "../../players/breaks";
import { attachPlaybackTelemetry, type PlayerEvent, type PlayerEventLevel } from "../../players/telemetry";
import { superviseTicketedSession } from "../../players/ticketed";
import type { Source } from "../playback";
import { sessionIdOf } from "../playback";

/** One load attempt of the multivariant playlist, no retry (hls.js retries it by default). */
const SINGLE_LOAD = {
  default: { maxTimeToFirstByteMs: 10_000, maxLoadTimeMs: 20_000, timeoutRetry: null, errorRetry: null },
};

export interface ViewerPlayerProps {
  /** One new session's source; called for every open and reopen (a ticket is single-use). */
  readonly openSource: (signal: AbortSignal) => Promise<Source>;
  readonly cdnBaseUrl: string;
  readonly label: string;
  readonly onSource: (source: Source) => void;
  /** MediaTailor's session id, once the multivariant playlist names it. */
  readonly onSessionId: (sid: string) => void;
  readonly onBreak: (played: PlayedBreak) => void;
  readonly onEvent: (event: PlayerEvent) => void;
}

/**
 * The viewer's hls.js player of a ticketed SSAI session. It mints a new ticket for every open and reopen
 * (players/ticketed.ts), shows whether an ad or the channel's slate is playing, and reports each break as played.
 * Give it a `key` per channel: a session belongs to one channel.
 */
export function ViewerPlayer({ openSource, cdnBaseUrl, label, onSource, onSessionId, onBreak, onEvent }: ViewerPlayerProps) {
  const video = useRef<HTMLVideoElement>(null);
  const [phase, setPhase] = useState<BreakPhase>({ kind: "content" });
  const emit = useEffectEvent((level: PlayerEventLevel, name: string, detail: string) => {
    onEvent({ level, name, detail });
  });
  const source = useEffectEvent(async (signal: AbortSignal) => {
    const opened = await openSource(signal);
    if (!signal.aborted) {
      onSource(opened);
      if (opened.sid) {
        onSessionId(opened.sid);
      }
    }
    return opened.url;
  });
  const sessionId = useEffectEvent((sid: string) => {
    onSessionId(sid);
  });
  const played = useEffectEvent((value: PlayedBreak) => {
    onBreak(value);
  });

  useEffect(() => {
    const media = video.current;
    if (!media) {
      return;
    }
    if (!Hls.isSupported()) {
      const controller = new AbortController();
      void source(controller.signal).then((url) => {
        if (!controller.signal.aborted) {
          media.src = url;
          emit("info", "Native playback", "No Media Source Extensions: ad events cannot be observed");
        }
      });
      return () => {
        controller.abort();
        media.removeAttribute("src");
        media.load();
      };
    }
    return superviseTicketedSession(
      source,
      (url) => {
        const hls = new Hls({
          enableInterstitialPlayback: false,
          abrEwmaDefaultEstimate: 2_000_000,
          // Never load a ticketed URL twice: MediaTailor creates the session, and its PRE_SESSION redeem binds the
          // ticket, even when it answers the player an error (a starting channel's 404 or 504), so a retry of the
          // same URL would be a replay, an anonymous session. The supervisor reopens on a fresh ticket instead.
          manifestLoadPolicy: SINGLE_LOAD,
        });
        attachPlaybackTelemetry(hls, {
          event: (level, name, detail) => {
            emit(level, name, detail);
          },
          adState: () => undefined,
        });
        attachBreakTelemetry(
          hls,
          {
            phase: setPhase,
            played: (value) => {
              emit("ad", "Break played", `${value.ads} ad(s), ${Math.round(value.slateSeconds)} s of slate`);
              played(value);
            },
          },
          cdnBaseUrl,
        );
        hls.on(Events.MANIFEST_PARSED, (_event, { levels }) => {
          const sid = levels.map((level) => sessionIdOf(level.uri)).find(Boolean);
          if (sid) {
            sessionId(sid);
          }
        });
        hls.loadSource(url);
        hls.attachMedia(media);
        return hls;
      },
      {
        onOpen: (url, attempt) => {
          setPhase({ kind: "content" });
          const how = url.includes("ads.t=") ? "On a new playback ticket" : "Anonymous: contextual ads";
          emit("info", attempt === 0 ? "Session opened" : "Session reopened", how);
        },
        onRetry: ({ attempt, delayMs, reason }) => {
          setPhase({ kind: "content" });
          emit("warning", `Retry ${attempt}`, `${reason}: a new session in ${delayMs / 1000} s`);
        },
        onFatal: (reason) => {
          emit("error", "Cannot play", reason);
        },
      },
    );
  }, [cdnBaseUrl]);

  return (
    <div className={styles.player}>
      <video ref={video} className={styles.video} controls muted autoPlay playsInline aria-label={label} />
      <div className={styles.badge}>
        <PhaseBadge phase={phase} />
      </div>
    </div>
  );
}

function PhaseBadge({ phase }: { phase: BreakPhase }) {
  switch (phase.kind) {
    case "content":
      return <Badge color="grey">Program</Badge>;
    case "ad":
      return <Badge color="red">{`Ad ${phase.index}`}</Badge>;
    case "slate":
      return <Badge color="blue">Slate</Badge>;
  }
}
