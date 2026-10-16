import { describe, expect, it } from "vitest";
import { breakSummary, GUIDE_PAST_MS, guidePrograms, guideStatus, type EpgProgram } from "./schedule";

const NOW = Date.parse("2026-10-16T07:00:00Z");
const MINUTE = 60_000;

/** A program from `fromMin` to `toMin` minutes relative to NOW. */
const program = (name: string, fromMin: number, toMin: number, status: EpgProgram["status"]): EpgProgram => ({
  name,
  title: name,
  asset_id: `${name}-a3f1`,
  start: new Date(NOW + fromMin * MINUTE).toISOString(),
  end: new Date(NOW + toMin * MINUTE).toISOString(),
  genres: [],
  breaks: [],
  status,
});

describe("guideStatus", () => {
  it("shows the program scheduled now as on air while the channel runs", () => {
    expect(guideStatus(program("current", -5, 5, "PUBLISHED"), NOW)).toBe("ON_AIR");
    expect(guideStatus(program("next", 5, 15, "PUBLISHED"), NOW)).toBe("PUBLISHED");
    expect(guideStatus(program("ended", -15, -5, "AIRED"), NOW)).toBe("AIRED");
  });

  it("shows nothing on air while the channel is off air", () => {
    expect(guideStatus(program("current", -5, 5, "PUBLISHED"), undefined)).toBe("PUBLISHED");
  });

  it("never shows a skipped program as on air: filler plays in its slot", () => {
    expect(guideStatus(program("skipped", -5, 5, "SKIPPED"), NOW)).toBe("SKIPPED");
  });
});

describe("guidePrograms", () => {
  it("lists programs that have not ended and the ones skipped in the guide's past half hour", () => {
    const programs = [
      program("skipped-long-ago", -60, -40, "SKIPPED"),
      program("aired", -40, -20, "AIRED"),
      program("skipped-at-start", -20, -2, "SKIPPED"),
      program("current", -2, 10, "PUBLISHED"),
      program("next", 10, 40, "PUBLISHED"),
    ];
    expect(guidePrograms(programs, NOW).map((p) => p.name)).toEqual(["skipped-at-start", "current", "next"]);
    // Half an hour after its planned end, a skipped program leaves the table like the others.
    expect(guidePrograms(programs, NOW - 2 * MINUTE + GUIDE_PAST_MS).map((p) => p.name)).toEqual(["next"]);
  });
});

describe("breakSummary", () => {
  it("counts equal breaks with their length and totals unequal ones", () => {
    expect(breakSummary([])).toBe("None");
    expect(breakSummary([{ offset_ms: 300_300, duration_ms: 60_060 }])).toBe("1 × 1:00");
    expect(breakSummary([{ offset_ms: 1, duration_ms: 60_060 }, { offset_ms: 2, duration_ms: 90_090 }])).toBe("2, 2:30 in all");
  });
});
