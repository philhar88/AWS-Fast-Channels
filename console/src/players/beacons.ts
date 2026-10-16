/**
 * Client-side ad tracking for SGAI. Preconditioned SGAI ad playlists carry no server-side beacon hooks, so the
 * player requests `aws.reportingMode=CLIENT` and fires the beacons itself (_review.md, SGAI with preconditioned
 * ads; e2e.md §5.5). MediaTailor then adds a `TRACKING` block to each asset list, with the same schema as the SSAI
 * tracking API (ad-insertion.md §3.3): `TRACKING.avails[].ads[i]` describes `ASSETS[i]`.
 */
import type Hls from "hls.js";
import { Events } from "hls.js";

export interface TrackingEvent {
  readonly eventType: string;
  readonly startTimeInSeconds: number;
  readonly beaconUrls: readonly string[];
}

export interface TrackedAd {
  readonly adId?: string;
  readonly creativeId?: string;
  readonly durationInSeconds?: number;
  readonly startTimeInSeconds: number;
  readonly trackingEvents: readonly TrackingEvent[];
}

/** One beacon group, due `offset` seconds after its ad starts. */
export interface Beacon {
  readonly eventType: string;
  readonly offset: number;
  readonly urls: readonly string[];
}

interface ClientTracking {
  readonly TRACKING?: { readonly avails?: readonly { readonly ads?: readonly TrackedAd[] }[] };
}

/** The tracked ad behind asset `index` of an asset-list response, if the session uses client-side reporting. */
export function trackedAd(assetList: unknown, index: number): TrackedAd | undefined {
  const ads = (assetList as ClientTracking | null)?.TRACKING?.avails?.flatMap((avail) => avail.ads ?? []) ?? [];
  return ads[index];
}

/** Beacons of an ad in firing order; offsets are relative to the ad's own start. */
export function beaconSchedule(ad: TrackedAd): Beacon[] {
  return ad.trackingEvents
    .map((event) => ({
      eventType: event.eventType,
      offset: Math.max(0, event.startTimeInSeconds - ad.startTimeInSeconds),
      urls: event.beaconUrls,
    }))
    .sort((a, b) => a.offset - b.offset);
}

/** Number of leading beacons in `schedule` that are due at `elapsed` seconds into the ad. */
export const dueCount = (schedule: readonly Beacon[], elapsed: number): number =>
  schedule.filter((beacon) => beacon.offset <= elapsed).length;

/** Expands the VAST 4 macros a pod may use in beacon URLs. */
export function expandMacros(url: string, now = new Date()): string {
  return url
    .replaceAll("[CACHEBUSTING]", String(Math.floor(10_000_000 + Math.random() * 89_999_999)))
    .replaceAll("[TIMESTAMP]", encodeURIComponent(now.toISOString()));
}

/** Fire-and-forget GET; `no-cors` because third-party ad servers rarely send CORS headers for pixels. */
export function sendBeacon(url: string): void {
  void fetch(expandMacros(url), { mode: "no-cors", credentials: "omit", cache: "no-store", keepalive: true }).catch(
    () => undefined,
  );
}

/**
 * Fires the TRACKING beacons of each interstitial asset as it plays: impression/start when the asset starts, the
 * quartiles as its playhead passes their offsets, and whatever is left (complete) when the asset ends. An asset
 * that fails keeps its remaining beacons unsent. Listeners go away with the hls.js instance.
 */
export function attachBeaconEmitter(
  hls: Hls,
  media: HTMLMediaElement,
  onBeacon: (beacon: Beacon, ad: TrackedAd) => void,
  send: (url: string) => void = sendBeacon,
): void {
  let active: { ad: TrackedAd; schedule: Beacon[]; fired: number; elapsed: () => number } | null = null;

  const fireUpTo = (count: number) => {
    if (!active) {
      return;
    }
    for (const beacon of active.schedule.slice(active.fired, count)) {
      beacon.urls.forEach(send);
      onBeacon(beacon, active.ad);
    }
    active.fired = Math.max(active.fired, count);
  };
  const tick = () => {
    if (active) {
      fireUpTo(dueCount(active.schedule, active.elapsed()));
    }
  };

  hls.on(Events.INTERSTITIAL_ASSET_STARTED, (_event, { event, assetListIndex, player }) => {
    const ad = trackedAd(event.assetListResponse, assetListIndex);
    active = ad ? { ad, schedule: beaconSchedule(ad), fired: 0, elapsed: () => player.currentTime } : null;
    tick();
  });
  hls.on(Events.INTERSTITIAL_ASSET_ENDED, () => {
    fireUpTo(active?.schedule.length ?? 0);
    active = null;
  });
  hls.on(Events.INTERSTITIAL_ASSET_ERROR, () => {
    active = null;
  });
  media.addEventListener("timeupdate", tick);
  hls.once(Events.DESTROYING, () => {
    media.removeEventListener("timeupdate", tick);
  });
}
