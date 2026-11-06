/**
 * Supervises SSAI sessions whose URL is single-use. A playback ticket (`POST /playback`) is good for one MediaTailor
 * session: a second session opened on the same URL plays anonymously, with contextual ads (DESIGN §5.1). So every
 * open, and every reopen after an error, first asks `source` for a fresh URL, then opens hls.js on it.
 *
 * The backoff is `superviseSession`'s (players/retry.ts): 2, 4, 8, 16 s, then 30 s, reset by buffered media. A failed
 * mint is retried the same way, unless `source` throws a {@link FatalSourceError} (a session that needs a new
 * sign-in, a channel that has no configuration for the profile). Closing aborts a mint in flight, so a channel
 * change or an unmount during a mint never attaches a player to a detached video.
 */
import { Events } from "hls.js";
import { errorMessage } from "../lib/errors";
import { describeError, isRetryable, retryDelayMs, type Session } from "./retry";

/** A source failure that a retry cannot fix; the supervisor stops and reports it. */
export class FatalSourceError extends Error {
  override readonly name = "FatalSourceError";
}

export interface TicketedRetry {
  /** 1 for the first reopen after the session last buffered media. */
  readonly attempt: number;
  readonly delayMs: number;
  /** What failed: the hls.js error, or why the mint failed. */
  readonly reason: string;
}

export interface TicketedCallbacks {
  /** A session opened on `url`; `attempt` is 0 for the first open. */
  readonly onOpen?: (url: string, attempt: number) => void;
  /** A reopen was scheduled. */
  readonly onRetry: (retry: TicketedRetry) => void;
  /** The source failed for good; nothing more is opened. */
  readonly onFatal: (reason: string) => void;
}

/**
 * Asks `source` for a URL, opens a session on it with `open`, and repeats both after every retryable error.
 *
 * @returns closes the session, aborts a mint in flight and cancels a pending reopen
 */
export function superviseTicketedSession(
  source: (signal: AbortSignal) => Promise<string>,
  open: (url: string) => Session,
  callbacks: TicketedCallbacks,
): () => void {
  let attempt = 0;
  let closed = false;
  let pending: ReturnType<typeof setTimeout> | undefined;
  let mint: AbortController | undefined;
  let current: Session | undefined;

  const schedule = (reason: string) => {
    if (closed || pending !== undefined) {
      return;
    }
    attempt += 1;
    const delayMs = retryDelayMs(attempt);
    pending = setTimeout(() => {
      pending = undefined;
      current?.destroy();
      current = undefined;
      start();
    }, delayMs);
    callbacks.onRetry({ attempt, delayMs, reason });
  };

  const start = () => {
    const controller = new AbortController();
    mint = controller;
    source(controller.signal).then(
      (url) => {
        if (closed || controller.signal.aborted) {
          return;
        }
        const session = open(url);
        current = session;
        callbacks.onOpen?.(url, attempt);
        session.on(Events.FRAG_BUFFERED, () => {
          attempt = 0;
        });
        session.on(Events.ERROR, (_event, error) => {
          if (isRetryable(error)) {
            schedule(describeError(error));
          }
        });
      },
      (error: unknown) => {
        if (closed || controller.signal.aborted) {
          return;
        }
        if (error instanceof FatalSourceError) {
          callbacks.onFatal(error.message);
          return;
        }
        schedule(errorMessage(error));
      },
    );
  };
  start();

  return () => {
    closed = true;
    mint?.abort();
    clearTimeout(pending);
    pending = undefined;
    current?.destroy();
    current = undefined;
  };
}
