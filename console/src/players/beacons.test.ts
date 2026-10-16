import { Events } from "hls.js";
import type Hls from "hls.js";
import { describe, expect, it } from "vitest";
import { attachBeaconEmitter, beaconSchedule, dueCount, expandMacros, trackedAd, type TrackedAd } from "./beacons";

const event = (eventType: string, startTimeInSeconds: number) => ({ eventType, startTimeInSeconds, beaconUrls: [`https://cdn/b.gif?evt=${eventType}&cb=[CACHEBUSTING]`] });

const AD: TrackedAd = {
  creativeId: "cr-a",
  startTimeInSeconds: 714.645,
  trackingEvents: [event("complete", 718.645), event("midpoint", 716.645), event("impression", 714.645), event("firstQuartile", 715.645), event("start", 714.645)],
};

const ASSET_LIST = { ASSETS: [{ URI: "a", DURATION: 4 }, { URI: "b", DURATION: 4 }], TRACKING: { avails: [{ ads: [AD, { ...AD, creativeId: "cr-b" }] }] } };

/** The slice of hls.js the emitter uses: on/once listeners that a test can fire. */
function fakeHls() {
  const listeners = new Map<string, ((event: string, data: unknown) => void)[]>();
  const add = (name: string, fn: (event: string, data: unknown) => void) => listeners.set(name, [...(listeners.get(name) ?? []), fn]);
  return {
    hls: { on: add, once: add } as unknown as Hls,
    fire: (name: string, data?: unknown) => {
      for (const fn of listeners.get(name) ?? []) {
        fn(name, data);
      }
    },
  };
}

describe("beacons", () => {
  it("finds the tracked ad of each asset and orders its beacons by offset", () => {
    expect(trackedAd(ASSET_LIST, 1)?.creativeId).toBe("cr-b");
    expect(trackedAd({ ASSETS: [] }, 0)).toBeUndefined();
    expect(beaconSchedule(AD).map((b) => [b.eventType, b.offset])).toEqual([
      ["impression", 0],
      ["start", 0],
      ["firstQuartile", 1],
      ["midpoint", 2],
      ["complete", 4],
    ]);
    expect(dueCount(beaconSchedule(AD), 1.5)).toBe(3);
  });

  it("expands VAST cache-busting and timestamp macros", () => {
    const url = expandMacros("https://x/b.gif?cb=[CACHEBUSTING]&t=[TIMESTAMP]", new Date("2026-10-16T12:00:00Z"));
    expect(url).toMatch(/^https:\/\/x\/b\.gif\?cb=\d{8}&t=2026-10-16T12%3A00%3A00\.000Z$/);
  });

  it("fires due beacons as the asset plays and the rest when it ends", () => {
    const { hls, fire } = fakeHls();
    const media = new EventTarget() as HTMLMediaElement;
    const player = { currentTime: 0 };
    const sent: string[] = [];
    const fired: string[] = [];
    attachBeaconEmitter(hls, media, (beacon) => fired.push(beacon.eventType), (url) => sent.push(url));

    fire(Events.INTERSTITIAL_ASSET_STARTED, { event: { assetListResponse: ASSET_LIST }, assetListIndex: 0, player });
    expect(fired).toEqual(["impression", "start"]);
    player.currentTime = 2.1;
    media.dispatchEvent(new Event("timeupdate"));
    expect(fired).toEqual(["impression", "start", "firstQuartile", "midpoint"]);
    fire(Events.INTERSTITIAL_ASSET_ENDED);
    expect(fired.at(-1)).toBe("complete");
    expect(sent).toHaveLength(5);

    fire(Events.INTERSTITIAL_ASSET_STARTED, { event: { assetListResponse: ASSET_LIST }, assetListIndex: 1, player: { currentTime: 0 } });
    fire(Events.INTERSTITIAL_ASSET_ERROR);
    fire(Events.INTERSTITIAL_ASSET_ENDED);
    expect(fired).toHaveLength(7); // impression + start of the failed asset, nothing more
  });
});
