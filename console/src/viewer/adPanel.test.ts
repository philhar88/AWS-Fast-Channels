import { describe, expect, it, vi } from "vitest";
import { AdPanelError, droppedLine, fetchDecisions, modeLine, podLine, policyLine, type BreakDecision } from "./adPanel";

const LIGHT_LOAD = { policy: "light", reason: "tier", max_ads: 1, bar_cpm: 20, slots: 2 };
const STANDARD_LOAD = { policy: "standard", reason: "control", max_ads: null, bar_cpm: 0, slots: 2 };

const LIGHT: BreakDecision = {
  decision_id: "sid.101",
  evid: "101",
  at: "2026-11-05T20:04:00Z",
  mode: "FULL",
  mode_reason: "bound",
  tier: "light",
  ad_load: LIGHT_LOAD,
  pod_reason: "partial",
  ads: [{ bidder: "amt", price_cpm: 35, house: false, deal: null, seconds: 30.03 }],
  filled_s: 30.03,
  break_s: 60.06,
  dropped: { below_bar: 3, ad_load: 1, won: 0 },
};

const STANDARD: BreakDecision = {
  ...LIGHT,
  tier: "standard",
  ad_load: STANDARD_LOAD,
  pod_reason: "filled",
  ads: [
    { bidder: "amt", price_cpm: 35, house: false, deal: null, seconds: 30.03 },
    { bidder: "ownbook", price_cpm: 0.01, house: true, deal: null, seconds: 30.03 },
  ],
  filled_s: 60.06,
  dropped: {},
};

describe("lines", () => {
  it("explains a light pod", () => {
    expect(policyLine(LIGHT)).toBe("Light ad load: at most 1 ad, only bids above $20.00 CPM");
    expect(podLine(LIGHT)).toBe("1 ad ($35.00 CPM) and 0:30 of slate");
    expect(droppedLine(LIGHT)).toBe("Not run: 3 below the tier's bar, 1 over the tier's ad cap");
    expect(modeLine(LIGHT)).toBe("FULL: its ticket is bound to this session");
    expect(modeLine({ ...LIGHT, mode: "CONTEXTUAL", mode_reason: "replay" })).toBe("CONTEXTUAL: a ticket already used by another session");
    expect(modeLine({ ...LIGHT, mode: "SAFE", mode_reason: "consent_stale" })).toBe("SAFE: consent stale");
    expect(modeLine({ ...LIGHT, mode: null })).toBe("Not recorded");
  });

  it("explains the control plan, the holdout, exemptions and the guardrail", () => {
    expect(policyLine(STANDARD)).toBe("Standard ad load: every slot at the market floor");
    expect(podLine(STANDARD)).toBe("2 ads ($35.00 CPM, house)");
    expect(droppedLine(STANDARD)).toBeNull();
    expect(policyLine({ ...STANDARD, tier: "holdout" })).toBe("Holdout: the control plan, every slot at the market floor");
    const contextual = { ...STANDARD, mode: "CONTEXTUAL", tier: null, ad_load: { ...STANDARD_LOAD, reason: "exempt" } };
    expect(policyLine(contextual)).toBe("CONTEXTUAL session: the control plan, whatever the tier");
    expect(policyLine({ ...LIGHT, ad_load: { ...LIGHT_LOAD, reason: "min_load" } })).toBe(
      "No bid cleared the $20.00 CPM bar, so the single best ad ran",
    );
    expect(policyLine({ ...LIGHT, ad_load: null })).toBe("No ad-load policy recorded");
    expect(podLine({ ...LIGHT, ads: [], filled_s: 0 })).toBe("No ads and 1:00 of slate");
  });
});

describe("fetchDecisions", () => {
  it("asks for one session's breaks with the access token", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve(new Response(JSON.stringify({ sid: "s", breaks: [LIGHT] }))));
    await expect(fetchDecisions("https://panel.example", "tok", "s", undefined, fetchImpl)).resolves.toEqual({ sid: "s", breaks: [LIGHT] });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://panel.example/sessions/s/breaks");
    expect(init.headers).toEqual({ Authorization: "Bearer tok" });
  });

  it("raises the API's message", async () => {
    const fetchImpl = () => Promise.resolve(new Response(JSON.stringify({ message: "Not your session" }), { status: 403 }));
    await expect(fetchDecisions("https://panel.example/", "tok", "s", undefined, fetchImpl)).rejects.toEqual(
      new AdPanelError("Not your session", 403),
    );
  });
});
