/**
 * Turns hls.js events into an operator-readable ad log for the SSAI vs SGAI comparison (ladders.md §7):
 * stitched ads are only visible as segment URLs behind discontinuities, guided ads as HLS Interstitials events.
 */
import type Hls from "hls.js";
import { Events } from "hls.js";
import { attachBeaconEmitter } from "./beacons";
import { describeError, isRetryable } from "./retry";

export type InsertionMode = "ssai" | "sgai";

export type AdState =
  | { readonly kind: "content" }
  | { readonly kind: "stitched" }
  | { readonly kind: "guided"; readonly asset: number; readonly of: number };

/** `warning`: the player lost the stream and reloads it (players/retry.ts). */
export type PlayerEventLevel = "info" | "ad" | "beacon" | "warning" | "error";

export interface PlayerEvent {
  readonly mode: InsertionMode;
  readonly level: PlayerEventLevel;
  readonly name: string;
  readonly detail: string;
}

export interface TelemetrySink {
  event(level: PlayerEventLevel, name: string, detail: string): void;
  adState(state: AdState): void;
}

/**
 * Stitched ad segments are recognised by their path below the CDN root: `/v1/segment/…` (server-side reporting,
 * 301 to the CDN), `/tm/…` (MediaTailor transcodes) and `/ads/…` or `/preconditioned-ads/…` (preconditioned
 * creatives); e2e.md §4.3, ad-insertion.md §4.2. Content segments live under `/assets/…`.
 */
const AD_PATH = /^\/(?:v1\/segment|tm|ads|preconditioned-ads)\//;

export function isAdSegment(url: string, cdnBaseUrl: string, page = window.location.href): boolean {
  const base = new URL(cdnBaseUrl, page);
  const target = new URL(url, base);
  const root = base.pathname.replace(/\/$/, "");
  const path = target.origin === base.origin && target.pathname.startsWith(`${root}/`) ? target.pathname.slice(root.length) : target.pathname;
  return AD_PATH.test(path);
}

const fileName = (url: string): string => new URL(url, window.location.href).pathname.split("/").slice(-2).join("/");

/** SSAI: ad state follows the playing fragment; one log line per transition into or out of a break. */
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

/** SGAI: the HLS Interstitials lifecycle, plus client-side beacons from the asset list's TRACKING block. */
export function attachSgaiTelemetry(hls: Hls, sink: TelemetrySink, media: HTMLMediaElement): void {
  let scheduled = -1;
  hls.on(Events.INTERSTITIALS_UPDATED, (_event, { events }) => {
    if (events.length !== scheduled) {
      scheduled = events.length;
      sink.event("info", "INTERSTITIALS_UPDATED", `${events.length} interstitial(s) in the schedule`);
    }
  });
  hls.on(Events.ASSET_LIST_LOADED, (_event, { event, assetListResponse }) => {
    const tracking = "TRACKING" in assetListResponse ? ", client-side tracking" : "";
    sink.event("info", "ASSET_LIST_LOADED", `${assetListResponse.ASSETS.length} ad(s) for break ${event.identifier}${tracking}`);
  });
  hls.on(Events.INTERSTITIAL_STARTED, (_event, { event }) => {
    sink.event("ad", "INTERSTITIAL_STARTED", `Break ${event.identifier}, ${event.duration.toFixed(1)} s`);
  });
  hls.on(Events.INTERSTITIAL_ASSET_STARTED, (_event, { event, asset, assetListIndex }) => {
    const of = event.assetList.length;
    sink.adState({ kind: "guided", asset: assetListIndex + 1, of });
    const duration = asset.duration === null ? "" : `, ${asset.duration.toFixed(1)} s`;
    sink.event("ad", "INTERSTITIAL_ASSET_STARTED", `Ad ${assetListIndex + 1} of ${of}${duration}`);
  });
  hls.on(Events.INTERSTITIAL_ASSET_ENDED, (_event, { event, assetListIndex }) => {
    sink.event("ad", "INTERSTITIAL_ASSET_ENDED", `Ad ${assetListIndex + 1} of ${event.assetList.length}`);
  });
  hls.on(Events.INTERSTITIAL_ENDED, (_event, { event }) => {
    sink.event("ad", "INTERSTITIAL_ENDED", `Break ${event.identifier}`);
  });
  hls.on(Events.INTERSTITIALS_PRIMARY_RESUMED, () => {
    sink.adState({ kind: "content" });
    sink.event("info", "INTERSTITIALS_PRIMARY_RESUMED", "Content resumed");
  });
  hls.on(Events.INTERSTITIAL_ASSET_ERROR, (_event, { details, error }) => {
    sink.event("error", "INTERSTITIAL_ASSET_ERROR", `${details}: ${error.message}`);
  });
  attachBeaconEmitter(hls, media, (beacon, ad) => {
    sink.event("beacon", `Beacon ${beacon.eventType}`, `${ad.creativeId ?? ad.adId ?? "ad"} · ${beacon.urls.length} URL(s)`);
  });
}

/**
 * Both modes: what was loaded and which rendition plays (the codec selector's effect), and fatal errors the player
 * does not recover from. Retryable ones are logged with their reload (`superviseSession`).
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
  hls.on(Events.SUBTITLE_TRACKS_UPDATED, (_event, { subtitleTracks }) => {
    const names = subtitleTracks.map((track) => `${track.name} (${track.lang ?? "und"})`);
    sink.event("info", "SUBTITLE_TRACKS_UPDATED", names.length ? `Captions: ${names.join(", ")}` : "No caption tracks");
  });
  hls.on(Events.SUBTITLE_TRACK_SWITCH, (_event, { id }) => {
    const track = hls.subtitleTracks[id];
    sink.event("info", "SUBTITLE_TRACK_SWITCH", track ? `Captions on: ${track.name}` : "Captions off");
  });
  hls.on(Events.ERROR, (_event, error) => {
    if (error.fatal && !isRetryable(error)) {
      sink.event("error", "ERROR", `${error.type}: ${describeError(error)}`);
    }
  });
}

/** Shows the first caption track (IMSC1, one per ladder language) or none; hls.js renders it as TextTrack cues. */
export function showCaptions(hls: Pick<Hls, "subtitleTrack" | "subtitleDisplay" | "subtitleTracks">, on: boolean): void {
  hls.subtitleDisplay = on;
  hls.subtitleTrack = on && hls.subtitleTracks.length > 0 ? 0 : -1;
}
