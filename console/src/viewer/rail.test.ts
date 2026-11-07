import { describe, expect, it } from "vitest";
import type { Epg, EpgProgram } from "../lib/schedule";
import type { ViewerChannel } from "./config";
import { affinity, breakWindows, channelRail, genreWeights, IN_BREAK_FACTOR, programRail, type ChannelGuide } from "./rail";
import sharedFixture from "../../../contracts/fixtures/rail/program-rail.json?raw";

const T0 = Date.parse("2026-11-05T20:00:00Z");
const MIN = 60_000;

const program = (title: string, genres: string[], startMin: number, minutes: number, breaksAtMin: number[] = []): EpgProgram => ({
  name: `${title}-${startMin}`,
  title,
  asset_id: title,
  start: new Date(T0 + startMin * MIN).toISOString(),
  end: new Date(T0 + (startMin + minutes) * MIN).toISOString(),
  genres,
  breaks: breaksAtMin.map((at) => ({ offset_ms: at * MIN, duration_ms: MIN })),
  status: "PUBLISHED",
});

const channel = (name: string): ViewerChannel => ({ name, monetized: true, playback: { hls: `https://cdn.example/${name}` } });
const guide = (name: string, programs: EpgProgram[]): ChannelGuide => ({
  channel: channel(name),
  epg: { channel: name, generated_at: "", ladder_version: "v1", programs } satisfies Epg,
});

const SCIFI = guide("fastm-demo", [
  program("Tears of Steel", ["sci-fi", "action", "live-action"], 0, 14, [4, 8]),
  program("Sintel", ["animation", "fantasy", "adventure"], 14, 17),
]);
const ANIMATION = guide("fastm-anim", [
  program("Elephants Dream", ["animation", "surreal"], 0, 12, [3]),
  program("Sintel", ["animation", "fantasy", "adventure"], 12, 17),
]);
const DANA = ["sci-fi", "action", "fantasy"];
const LUIS = ["animation", "surreal"];

describe("affinity", () => {
  it("weights genres by rank and reports them in the persona's order", () => {
    const weights = genreWeights(DANA);
    expect([...weights.values()]).toEqual([1, 2 / 3, 1 / 2]);
    expect(affinity(SCIFI.epg?.programs[0], weights)).toEqual({ score: 1 + 2 / 3, matched: ["sci-fi", "action"] });
    expect(affinity(undefined, weights)).toEqual({ score: 0, matched: [] });
  });
});

describe("breakWindows", () => {
  it("places each break at its content offset plus the breaks before it", () => {
    const [first, second] = breakWindows(program("x", [], 0, 14, [8, 4]));
    expect(first).toEqual({ start: T0 + 4 * MIN, end: T0 + 5 * MIN });
    expect(second).toEqual({ start: T0 + 9 * MIN, end: T0 + 10 * MIN });
  });
});

describe("channelRail", () => {
  it("ranks the channels differently for different personas", () => {
    const now = T0 + MIN;
    const dana = channelRail(DANA, [ANIMATION, SCIFI], now);
    const luis = channelRail(LUIS, [ANIMATION, SCIFI], now);
    expect(dana.map((t) => t.channel.name)).toEqual(["fastm-demo", "fastm-anim"]);
    expect(luis.map((t) => t.channel.name)).toEqual(["fastm-anim", "fastm-demo"]);
    expect(dana[0]?.reasons[0]).toBe("You like sci-fi and action: Tears of Steel is on now");
    expect(dana[0]?.next?.title).toBe("Sintel");
  });

  it("ranks a channel in a break down and says when it is back", () => {
    const now = T0 + 4 * MIN + 25_000; // 35 s left in Tears of Steel's first break
    const [top, second] = channelRail(DANA, [SCIFI, ANIMATION], now);
    const quiet = channelRail(DANA, [SCIFI, ANIMATION], T0 + MIN)[0];
    expect(top?.channel.name).toBe("fastm-demo");
    expect(top?.score).toBeCloseTo((quiet?.score ?? 0) * IN_BREAK_FACTOR);
    expect(top?.breakEndsAt).toBe(T0 + 5 * MIN);
    expect(top?.reasons.at(-1)).toBe("In an ad break, back in 0:35: ranked down");
    expect(second?.breakEndsAt).toBeUndefined();
  });

  it("orders a guest's channels by what is on now, then by name", () => {
    const empty = guide("fastm-aaa", []);
    const rail = channelRail([], [SCIFI, empty, ANIMATION], T0 + MIN);
    expect(rail.map((t) => [t.channel.name, t.reasons[0]])).toEqual([
      ["fastm-anim", "On now"],
      ["fastm-demo", "On now"],
      ["fastm-aaa", "Nothing on now"],
    ]);
  });

  it("leaves out skipped programs", () => {
    const skipped = guide("fastm-demo", [{ ...program("Tears of Steel", ["sci-fi"], 0, 14), status: "SKIPPED" }]);
    expect(channelRail(DANA, [skipped], T0 + MIN)[0]?.now).toBeUndefined();
  });
});

describe("programRail", () => {
  it("lists matching programs, best match first, then soonest, each title once", () => {
    const rail = programRail(LUIS, [SCIFI, ANIMATION], T0 + MIN);
    expect(rail.map((t) => [t.program.title, t.channel.name, t.onNow])).toEqual([
      ["Elephants Dream", "fastm-anim", true],
      ["Sintel", "fastm-anim", false],
    ]);
    expect(programRail([], [SCIFI], T0 + MIN)).toHaveLength(2);
    expect(programRail(DANA, [SCIFI, ANIMATION], T0 + MIN, 1).map((t) => t.program.title)).toEqual(["Tears of Steel"]);
  });
});

/** contracts/fixtures/rail/program-rail.json: the same cases run against the recommender's Python port. */
interface SharedCase {
  readonly name: string;
  readonly genres: readonly string[];
  readonly now: string;
  readonly guides: readonly string[];
  readonly limit?: number;
  readonly expected: readonly { channel: string; name: string }[];
}
const SHARED = JSON.parse(sharedFixture) as { cases: SharedCase[]; epg: Record<string, EpgProgram[]> };

describe("programRail on the shared fixture", () => {
  it.each(SHARED.cases.map((c) => [c.name, c] as const))("%s", (_, c) => {
    const guides = c.guides.map((name) => guide(name, SHARED.epg[name] ?? []));
    const rail = programRail(c.genres, guides, Date.parse(c.now), c.limit);
    expect(rail.map((t) => ({ channel: t.channel.name, name: t.program.name }))).toEqual(c.expected);
  });
});
