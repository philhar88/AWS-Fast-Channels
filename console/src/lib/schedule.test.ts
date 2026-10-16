import { describe, expect, it } from "vitest";
import { breakSummary } from "./schedule";

describe("breakSummary", () => {
  it("counts equal breaks with their length and totals unequal ones", () => {
    expect(breakSummary([])).toBe("None");
    expect(breakSummary([{ offset_ms: 300_300, duration_ms: 60_060 }])).toBe("1 × 1:00");
    expect(breakSummary([{ offset_ms: 1, duration_ms: 60_060 }, { offset_ms: 2, duration_ms: 90_090 }])).toBe("2, 2:30 in all");
  });
});
