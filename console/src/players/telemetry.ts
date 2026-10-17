/**
 * Turns hls.js events into an operator-readable ad log (ladders.md §7). MediaTailor stitches the ads into the
 * channel's playlists (SSAI), so a player sees them only as ad segment URLs behind discontinuities.
 */
import type Hls from "hls.js";
import { Events } from "hls.js";
import { describeError, isRetryable } from "./retry";

export type AdState = { readonly kind: "content" } | { readonly kind: "stitched" };

/** `warning`: the player lost the stream and reloads it (players/retry.ts). */
export type PlayerEventLevel = "info" | "ad" | "warning" | "error";

export interface PlayerEvent {
  readonly level: PlayerEventLevel;
  readonly name: string;
  readonly detail: string;
}

export interface TelemetrySink {
  event(level: PlayerEventLevel, name: string, detail: string): void;
  adState(state: AdState): void;
}

/**
 * Stitched ad segments are recognised by their path below the CDN root: `/v1/segment/…` (server-side reporting;
 * MediaTailor answers with a 301 to the creative on the CDN), `/tm/…` (ads MediaTailor transcoded) and `/ads/…`
 * (preconditioned house ads on the CDN); e2e.md §4.3, ad-insertion.md §4.2. Content segments live under `/assets/…`.
 */
const AD_PATH = /^\/(?:v1\/segment|tm|ads)\//;

export function isAdSegment(url: string, cdnBaseUrl: string, page = window.location.href): boolean {
  const base = new URL(cdnBaseUrl, page);
  const target = new URL(url, base);
  const root = base.pathname.replace(/\/$/, "");
  const path = target.origin === base.origin && target.pathname.startsWith(`${root}/`) ? target.pathname.slice(root.length) : target.pathname;
  return AD_PATH.test(path);
}

const fileName = (url: string): string => new URL(url, window.location.href).pathname.split("/").slice(-2).join("/");

/** Ad state follows the playing fragment; one log line per transition into or out of a break. */
export function attachSsaiTelemetry(hls: Hls, sink: TelemetrySink, cdnBaseUrl: string): void {
  let inAd = false;
  hls.on(Events.FRAG_CHANGED, (_event, { frag }) => {
    const ad = isAdSegment(frag.url, cdnBaseUrl);
    if (ad === inAd) {
      return;
    }
    inAd = ad;
    sink.adState(ad ? { kind: "stitched" } : { kind: "content" });
    sink.event("ad", ad ? "Stitched ad started" : "Stitched ad ended", ad ? fileName(frag.url) : "Content resumed");
  });
}

/**
 * What was loaded and which rendition plays, and fatal errors the player does not recover from. Retryable ones are
 * logged with their reload (`superviseSession`).
 */
export function attachPlaybackTelemetry(hls: Hls, sink: TelemetrySink): void {
  hls.on(Events.MANIFEST_PARSED, (_event, { levels }) => {
    const codecs = [...new Set(levels.map((level) => level.videoCodec?.split(".")[0]).filter(Boolean))];
    sink.event("info", "MANIFEST_PARSED", `${levels.length} variant(s)${codecs.length ? `: ${codecs.join(", ")}` : ""}`);
  });
  hls.on(Events.LEVEL_SWITCHED, (_event, { level }) => {
    const variant = hls.levels[level];
    if (variant) {
      const size = variant.height ? `${variant.width}×${variant.height} ` : "";
      const rate = variant.bitrate ? ` ${Math.round(variant.bitrate / 1000)} kbit/s` : "";
      sink.event("info", "LEVEL_SWITCHED", `${size}${variant.videoCodec ?? "video"}${rate}`);
    }
  });
  hls.on(Events.ERROR, (_event, error) => {
    if (error.fatal && !isRetryable(error)) {
      sink.event("error", "ERROR", `${error.type}: ${describeError(error)}`);
    }
  });
}
