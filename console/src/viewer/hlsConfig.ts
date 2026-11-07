/** The hls.js options of the viewer's player (components/ViewerPlayer.tsx). */
import type { HlsConfig } from "hls.js";

/** One load attempt of the multivariant playlist, no retry (hls.js retries it by default). */
export const SINGLE_LOAD = {
  default: { maxTimeToFirstByteMs: 10_000, maxLoadTimeMs: 20_000, timeoutRetry: null, errorRetry: null },
};

export const VIEWER_HLS_CONFIG = {
  // The playback configuration is STITCHED_ONLY: ads are segments in the playlist, never HLS Interstitials.
  enableInterstitialPlayback: false,
  // Desktop/TV start estimate: with its 500 kbit/s default, hls.js would start every session on the lowest rung.
  abrEwmaDefaultEstimate: 2_000_000,
  // Never load a ticketed URL twice: MediaTailor creates the session, and its PRE_SESSION redeem binds the ticket,
  // even when it answers the player an error (a starting channel's 404 or 504), so a retry of the same URL would be a
  // replay, an anonymous session. The supervisor reopens on a fresh ticket instead.
  manifestLoadPolicy: SINGLE_LOAD,
  // CMCD (CTA-5004) as a query parameter on every playlist and segment request: the CDN's logs parse it into their
  // cmcd-* fields (buffer length at a viewer's leave, for the tune-away labels) and keep it out of the cache keys.
  // The edge rebuild drops it from the session's init request. hls.js's CMCD session id is its own, not MediaTailor's:
  // the session id in the path stays the join key.
  cmcd: {},
} satisfies Partial<HlsConfig>;
