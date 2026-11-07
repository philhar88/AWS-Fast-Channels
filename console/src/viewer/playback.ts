/**
 * The URL a player opens, one per session. A signed-in viewer of a monetized channel gets a playback ticket:
 * `POST /playback {channel}` with the viewer's access token answers a single-use SSAI URL
 * (`…/v1/master/<hash>/<pc>/hls.m3u8?ads.t=<ticket>`) and the session's correlation id `cid`. A guest gets one from
 * `POST /playback/guest {channel, gpc?}`, with no token: their breaks are CONTEXTUAL, and opted out with Global
 * Privacy Control (the browser's `Sec-GPC` header, or `navigator.globalPrivacyControl`). An unmonetized channel, or
 * any failure of a mint, plays the anonymous URL instead: the channel's playback plan
 * (`<cdn>/plan/playback/<channel>.json`, the stable sibling) for a monetized channel, the v3.0.0 URL otherwise.
 * Anonymous sessions are CONTEXTUAL: contextual ads on the control plan, never an error.
 */
import { FatalSourceError } from "../players/ticketed";
import type { ViewerChannel } from "./config";

/** How a session was opened. */
export interface Source {
  readonly url: string;
  /** A ticketed session: the mint's correlation id. */
  readonly cid?: string;
  /** Why the session is anonymous, when it is. */
  readonly anonymous?: string;
  /** MediaTailor's session id when the source knows it up front (mock mode); otherwise the player reads it. */
  readonly sid?: string;
}

/** A failed mint: the API's error code (`not_released`, `unknown_channel`, …) and HTTP status. */
export class MintError extends Error {
  override readonly name = "MintError";
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const OUTPUT = "hls";

/** `POST <playbackUrl> {channel}` (the identity module's `POST /playback`) with the viewer's access token. */
export function mintTicket(
  playbackUrl: string,
  accessToken: string,
  channel: string,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<{ url: string; cid: string }> {
  return postTicket(
    playbackUrl,
    { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    { channel },
    signal,
    fetchImpl,
  );
}

/** `POST <playbackUrl>/guest {channel, gpc?}` (the identity module's `POST /playback/guest`), with no token. */
export function guestTicket(
  playbackUrl: string,
  channel: string,
  gpc: boolean,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<{ url: string; cid: string }> {
  return postTicket(`${playbackUrl}/guest`, { "Content-Type": "application/json" }, { channel, ...(gpc ? { gpc } : {}) }, signal, fetchImpl);
}

/** Whether the browser sends Global Privacy Control (`navigator.globalPrivacyControl`, where it is known). */
export function globalPrivacyControl(): boolean {
  return typeof navigator !== "undefined" && (navigator as { globalPrivacyControl?: unknown }).globalPrivacyControl === true;
}

async function postTicket(
  url: string,
  headers: Record<string, string>,
  payload: Record<string, unknown>,
  signal: AbortSignal | undefined,
  fetchImpl: typeof fetch,
): Promise<{ url: string; cid: string }> {
  const response = await fetchImpl(url, { method: "POST", headers, body: JSON.stringify(payload), signal });
  const body = (await response.json().catch(() => ({}))) as { url?: string; cid?: string; error?: string; message?: string };
  if (!response.ok || typeof body.url !== "string" || typeof body.cid !== "string") {
    const code = body.error ?? (response.status === 401 || response.status === 403 ? "unauthorized" : `http_${response.status}`);
    throw new MintError(body.message ?? `The playback API answered ${response.status} (${code})`, code, response.status);
  }
  return { url: body.url, cid: body.cid };
}

/** The monetized channel's anonymous URL from its playback plan (`playback.hls`). */
export async function plannedUrl(planBaseUrl: string, channel: string, signal?: AbortSignal, fetchImpl: typeof fetch = fetch): Promise<string> {
  const response = await fetchImpl(`${planBaseUrl}/${encodeURIComponent(channel)}.json`, { cache: "no-cache", signal });
  if (!response.ok) {
    throw new Error(`The playback plan of ${channel} returned HTTP ${response.status}`);
  }
  const plan = (await response.json()) as { playback?: Record<string, unknown> };
  const url = plan.playback?.[OUTPUT];
  if (typeof url !== "string") {
    throw new Error(`The playback plan of ${channel} has no ${OUTPUT} URL`);
  }
  return url;
}

export interface SourceOptions {
  readonly playbackUrl: string;
  readonly planBaseUrl: string;
  /** The signed-in viewer's access token; absent for a guest. */
  readonly accessToken?: () => Promise<string>;
  /** A guest's Global Privacy Control signal; the browser's own by default. */
  readonly gpc?: boolean;
  readonly fetchImpl?: typeof fetch;
}

/**
 * The URL for one new session of `channel` (call it once per open). The mint's authorization failures are fatal
 * (`FatalSourceError`): the viewer must sign in again, and playing anonymously would hide it.
 */
export async function openChannel(channel: ViewerChannel, options: SourceOptions, signal?: AbortSignal): Promise<Source> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const fallback = channel.playback[OUTPUT];
  if (!channel.monetized) {
    if (!fallback) {
      throw new FatalSourceError(`${channel.name} has no ${OUTPUT} output`);
    }
    return { url: fallback, anonymous: "not monetized" };
  }
  let anonymous: string;
  const signedIn = options.accessToken;
  try {
    const ticket = signedIn
      ? await mintTicket(options.playbackUrl, await signedIn(), channel.name, signal, fetchImpl)
      : await guestTicket(options.playbackUrl, channel.name, options.gpc ?? globalPrivacyControl(), signal, fetchImpl);
    return { url: ticket.url, cid: ticket.cid };
  } catch (error) {
    if (signal?.aborted) {
      throw error;
    }
    if (signedIn && error instanceof MintError && (error.status === 401 || error.status === 403)) {
      throw new FatalSourceError(`The playback API refused the session (${error.code}): sign in again`);
    }
    if (error instanceof MintError && error.code === "no_kids_configuration") {
      throw new FatalSourceError(error.message);
    }
    const why = error instanceof MintError ? `no ticket: ${error.code}` : "no ticket: the playback API is unreachable";
    anonymous = signedIn ? why : `guest; ${why}`;
  }
  try {
    return { url: await plannedUrl(options.planBaseUrl, channel.name, signal, fetchImpl), anonymous };
  } catch (error) {
    if (signal?.aborted || !fallback) {
      throw error;
    }
    return { url: fallback, anonymous: `${anonymous}; no playback plan` };
  }
}

/** MediaTailor's session id in a variant playlist URL (`…/v1/manifest/<hash>/<configuration>/<session id>/0.m3u8`). */
export function sessionIdOf(url: string): string | undefined {
  return /\/v1\/manifest\/(?:[^/]+\/)+?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\//i.exec(url)?.[1];
}
