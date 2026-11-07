import { afterEach, describe, expect, it, vi } from "vitest";
import { describeRail, fetchRail, RailError, type RailAnswer } from "./railClient";

const ANSWER: RailAnswer = {
  strategy: "llm",
  reason: "model",
  title: "Your sci-fi night",
  tiles: [
    {
      content_id: "tears-of-steel",
      title: "Tears of Steel",
      channel: "fastm-demo",
      program: "tears-of-steel-0",
      start: "2026-11-10T19:00:00Z",
      end: "2026-11-10T19:14:00Z",
      on_now: true,
      reason: "Sci-fi action, as you like it",
      evidence: ["declared_genre"],
    },
  ],
  lineage: { model: "us.anthropic.claude-haiku-5-5", prompt_version: "recs-v1", built_at: "2026-11-10T18:00:00Z" },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchRail", () => {
  it("sends the access token and the query, never caching", async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(ANSWER), { status: 200 }));
    vi.stubGlobal("fetch", fetch);

    const answer = await fetchRail("https://rail.example/rail", "token", { profile: "kids" });

    expect(answer).toEqual(ANSWER);
    const [url, init] = fetch.mock.calls[0] as [URL, RequestInit];
    expect(url.toString()).toBe("https://rail.example/rail?profile=kids");
    expect(init.headers).toEqual({ Authorization: "Bearer token" });
    expect(init.cache).toBe("no-store");
  });

  it("fails with the status, so the app falls back to its own rail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 503 })));

    await expect(fetchRail("https://rail.example/rail", "token")).rejects.toEqual(new RailError("GET /rail returned HTTP 503", 503));
  });
});

describe("describeRail", () => {
  it("names the model of a model rail and the reason of every other", () => {
    expect(describeRail(ANSWER)).toBe("Recommended by Claude Haiku 5.5: picked and explained by the recommender.");
    expect(describeRail({ ...ANSWER, strategy: "baseline", reason: "cap", lineage: null })).toBe(
      "Rule-based rail from your genres: the recommender's daily cap is reached.",
    );
    expect(describeRail({ ...ANSWER, strategy: "cohort", reason: "first_session" })).toContain("no personal data");
    expect(describeRail({ ...ANSWER, strategy: "kids", reason: "kids" })).toContain("kids lineup");
  });
});
