/**
 * Keeps a player's hls.js session alive across the errors a live Channel Assembly output produces: while a channel
 * is stopped or starting, MediaTailor answers the manifest with 504 ("Unable to obtain template playlist") or 404.
 * hls.js retries a few times itself (manifestLoadPolicy, playlistLoadPolicy), then raises a fatal network error and
 * stops for good. Only a new session recovers, so the supervisor opens one with exponential backoff.
 */
import { ErrorTypes, Events, type ErrorData } from "hls.js";
import type Hls from "hls.js";

export const RETRY_FIRST_MS = 2_000;
export const RETRY_MAX_MS = 30_000;

/** Wait before reload `attempt` (1-based): 2, 4, 8, 16 s, then 30 s for every further attempt. */
export const retryDelayMs = (attempt: number): number => Math.min(RETRY_FIRST_MS * 2 ** (attempt - 1), RETRY_MAX_MS);

/**
 * hls.js gave up loading the manifest, a media playlist or a segment (manifestLoadError, levelLoadError, their
 * timeouts, an empty or unparsable playlist).
 */
export const isRetryable = ({ fatal, type }: ErrorData): boolean => fatal && type === ErrorTypes.NETWORK_ERROR;

/** `manifestLoadError (HTTP 504)`: what failed, with the HTTP status when there was a response. */
export function describeError({ details, response }: ErrorData): string {
  return response?.code ? `${details} (HTTP ${response.code})` : details;
}

export interface Retry {
  /** 1 for the first reload after the session last buffered media. */
  readonly attempt: number;
  readonly delayMs: number;
  readonly error: ErrorData;
}

/** The part of an hls.js instance the supervisor uses. */
export type Session = Pick<Hls, "on" | "destroy">;

/**
 * Opens a session with `open` (create hls.js, load the source, attach the media) and reopens it after every
 * retryable error, {@link retryDelayMs} later; `onRetry` hears of each reload when it is scheduled. Buffered media
 * resets the backoff. The failed session stays until its replacement opens: hls.js has already stopped loading, and
 * destroying it inside its own error event would pull its state from under the listeners still running.
 *
 * @returns closes the session and cancels a pending reload
 */
export function superviseSession(open: () => Session, onRetry: (retry: Retry) => void): () => void {
  let attempt = 0;
  let pending: ReturnType<typeof setTimeout> | undefined;

  const start = (): Session => {
    const session = open();
    session.on(Events.FRAG_BUFFERED, () => {
      attempt = 0;
    });
    session.on(Events.ERROR, (_event, error) => {
      if (pending !== undefined || !isRetryable(error)) {
        return;
      }
      attempt += 1;
      const delayMs = retryDelayMs(attempt);
      pending = setTimeout(() => {
        pending = undefined;
        session.destroy();
        current = start();
      }, delayMs);
      onRetry({ attempt, delayMs, error });
    });
    return session;
  };
  let current = start();

  return () => {
    clearTimeout(pending);
    pending = undefined;
    current.destroy();
  };
}
