import { describe, expect, it } from "vitest";
import { BreakTracker, inBreakRange, type BreakRange, type PlayedFragment } from "./breaks";

const CDN = "https://cdn.example";
const PAGE = "https://watch.example/";
const START = Date.parse("2026-11-05T20:00:00Z");
const BREAK: BreakRange = {
  startDate: new Date(START + 12_000),
  duration: 60,
  plannedDuration: 60,
  attr: { "SCTE35-OUT": "0xFC30" },
};

/** A 6 s fragment `at` seconds after START, in discontinuity sequence `cc`. */
const frag = (path: string, at: number, cc = 0): PlayedFragment => ({
  url: `${CDN}${path}`,
  cc,
  duration: 6,
  programDateTime: START + at * 1000,
});

describe("inBreakRange", () => {
  it("is true inside an SCTE35-OUT range with a duration only", () => {
    expect(inBreakRange(START + 12_000, [BREAK])).toBe(true);
    expect(inBreakRange(START + 71_999, [BREAK])).toBe(true);
    expect(inBreakRange(START + 72_000, [BREAK])).toBe(false);
    expect(inBreakRange(START + 11_999, [BREAK])).toBe(false);
    expect(inBreakRange(null, [BREAK])).toBe(false);
    expect(inBreakRange(START + 20_000, [{ ...BREAK, attr: {} }])).toBe(false);
    expect(inBreakRange(START + 20_000, [{ ...BREAK, duration: null, plannedDuration: null }])).toBe(false);
    expect(inBreakRange(START + 20_000, [{ ...BREAK, duration: null }, undefined])).toBe(true);
  });
});

describe("BreakTracker", () => {
  it("counts one ad and the slate after it (a light pod)", () => {
    const tracker = new BreakTracker(CDN, PAGE);
    expect(tracker.observe(frag("/assets/p/000.ts", 6), [BREAK]).phase).toEqual({ kind: "content" });
    const phases = [12, 18, 24, 30, 36].map((at) => tracker.observe(frag("/v1/segment/h/sid/0/0", at, 1), [BREAK]).phase);
    expect(phases.at(-1)).toEqual({ kind: "ad", index: 1 });
    for (const at of [42, 48, 54, 60, 66]) {
      expect(tracker.observe(frag("/slate/000.ts", at, 2), [BREAK]).phase).toEqual({ kind: "slate" });
    }
    expect(tracker.observe(frag("/assets/p/001.ts", 72, 3), [BREAK])).toEqual({
      phase: { kind: "content" },
      ended: { ads: 1, adSeconds: 30, slateSeconds: 30 },
    });
  });

  it("counts each discontinuity of ads as the next ad (a full pod)", () => {
    const tracker = new BreakTracker(CDN, PAGE);
    for (const at of [12, 18, 24, 30, 36]) {
      tracker.observe(frag("/v1/segment/h/sid/0/0", at, 1), [BREAK]);
    }
    expect(tracker.observe(frag("/v1/segment/h/sid/0/1", 42, 2), [BREAK]).phase).toEqual({ kind: "ad", index: 2 });
    for (const at of [48, 54, 60, 66]) {
      tracker.observe(frag("/v1/segment/h/sid/0/1", at, 2), [BREAK]);
    }
    expect(tracker.observe(frag("/assets/p/001.ts", 72, 3), [BREAK]).ended).toEqual({ ads: 2, adSeconds: 60, slateSeconds: 0 });
  });

  it("reports nothing for content, and starts each break from zero", () => {
    const tracker = new BreakTracker(CDN, PAGE);
    expect(tracker.observe(frag("/assets/p/000.ts", 0), [BREAK]).ended).toBeUndefined();
    tracker.observe(frag("/ads/house/000.ts", 12, 1), [BREAK]);
    expect(tracker.observe(frag("/assets/p/001.ts", 72, 2), [BREAK]).ended?.ads).toBe(1);
    expect(tracker.observe(frag("/assets/p/002.ts", 78, 2), [BREAK]).ended).toBeUndefined();
    expect(tracker.observe(frag("/tm/cr/000.ts", 600, 3), []).phase).toEqual({ kind: "ad", index: 1 });
  });
});
