/**
 * The demo's "why these ads" panel: what the Pod Decision Service decided for each break of the viewer's own
 * session, from its decision records (`pds.policy` and `pds.select`, joined on `decision_id`). The viewer module's
 * panel API reads them with CloudWatch Logs Insights for one MediaTailor session id, and only for the signed-in
 * viewer's own sessions. Demo only (`modules.viewer.ad_panel`): a production viewer app shows no tier.
 */
import { formatDuration } from "../lib/format";

/** A tier's ad-load policy as the PDS applied it to one break (`pds.select` `ad_load`). */
export interface AdLoad {
  /** The tier whose policy ran: `light`, or `standard` for the control plan. */
  readonly policy: string;
  /** `tier` (the tier's policy), `control` (a control tier or none), `exempt` (KIDS, SAFE, CONTEXTUAL, a probe) or
   * `min_load` (no bid cleared the bar, so the control plan's single best ad ran). */
  readonly reason: string;
  /** At most this many ads (k*); `null` for no cap. */
  readonly max_ads: number | null;
  /** Bids must beat this CPM; 0 is the market floor. */
  readonly bar_cpm: number;
  /** 30 s slots the break offered. */
  readonly slots: number;
}

export interface PodAd {
  readonly bidder: string;
  readonly price_cpm: number;
  readonly house: boolean;
  readonly deal: string | null;
  readonly seconds: number | null;
}

export interface BreakDecision {
  readonly decision_id: string;
  readonly evid: string;
  /** When the PDS decided (ISO 8601): with prefetch, in the retrieval window before the break. */
  readonly at: string;
  /** `pds.policy`: the privacy mode and why. */
  readonly mode: string | null;
  readonly mode_reason: string | null;
  /** `pds.select`: the viewer's tier from the binding, `null` without one. */
  readonly tier: string | null;
  readonly ad_load: AdLoad | null;
  /** `filled`, `partial`, `empty`, `short_avail`. */
  readonly pod_reason: string | null;
  readonly ads: readonly PodAd[];
  readonly filled_s: number | null;
  readonly break_s: number | null;
  /** Bids that did not run, by outcome (`below_bar`, `ad_load`, …). */
  readonly dropped: Readonly<Record<string, number>>;
}

export interface SessionDecisions {
  readonly sid: string;
  /** Latest first. */
  readonly breaks: readonly BreakDecision[];
}

export class AdPanelError extends Error {
  override readonly name = "AdPanelError";
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** `GET <adPanelUrl>sessions/<sid>/breaks` with the viewer's access token. */
export async function fetchDecisions(
  adPanelUrl: string,
  accessToken: string,
  sid: string,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<SessionDecisions> {
  const base = adPanelUrl.endsWith("/") ? adPanelUrl : `${adPanelUrl}/`;
  const response = await fetchImpl(`${base}sessions/${encodeURIComponent(sid)}/breaks`, {
    headers: { Authorization: `Bearer ${accessToken}` },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { message?: string };
    throw new AdPanelError(body.message ?? `The ad panel answered HTTP ${response.status}`, response.status);
  }
  return (await response.json()) as SessionDecisions;
}

const MODE_REASONS: Readonly<Record<string, string>> = {
  bound: "its ticket is bound to this session",
  missing: "no ticket",
  invalid: "an invalid ticket",
  unknown: "an unknown ticket",
  replay: "a ticket already used by another session",
  pc_mismatch: "a ticket for another configuration",
  expired_or_raced: "an expired ticket",
};

/** The privacy mode and why, in one line: `FULL: its ticket is bound to this session`. */
export function modeLine(decision: BreakDecision): string {
  if (!decision.mode) {
    return "Not recorded";
  }
  const reason = decision.mode_reason && (MODE_REASONS[decision.mode_reason] ?? decision.mode_reason.replace(/_/g, " "));
  return reason ? `${decision.mode}: ${reason}` : decision.mode;
}

const cpm = (value: number) => `$${value.toFixed(2)} CPM`;
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The policy that applied to a break, in one line. */
export function policyLine(decision: BreakDecision): string {
  const load = decision.ad_load;
  if (!load) {
    return "No ad-load policy recorded";
  }
  switch (load.reason) {
    case "exempt":
      return `${decision.mode ?? "This"} session: the control plan, whatever the tier`;
    case "control":
      return decision.tier === "holdout"
        ? "Holdout: the control plan, every slot at the market floor"
        : "Standard ad load: every slot at the market floor";
    case "min_load":
      return `No bid cleared the ${cpm(load.bar_cpm)} bar, so the single best ad ran`;
    default: {
      const cap = load.max_ads === null ? "no cap on ads" : `at most ${plural(load.max_ads, "ad")}`;
      return `${load.policy[0]?.toUpperCase() ?? ""}${load.policy.slice(1)} ad load: ${cap}, only bids above ${cpm(load.bar_cpm)}`;
    }
  }
}

/** What aired in the break, in one line: `1 ad ($35.00 CPM) and 0:30 of slate`. */
export function podLine(decision: BreakDecision): string {
  const ads = decision.ads.length;
  const prices = decision.ads.map((ad) => (ad.house ? "house" : cpm(ad.price_cpm))).join(", ");
  const filled = decision.filled_s ?? 0;
  const slate = decision.break_s !== null ? Math.max(0, decision.break_s - filled) : 0;
  const parts = [ads === 0 ? "No ads" : `${plural(ads, "ad")} (${prices})`];
  if (slate >= 1) {
    parts.push(`${formatDuration(slate * 1000)} of slate`);
  }
  return parts.join(" and ");
}

/** The bids that did not run, in one line, or `null` when every bid ran. */
export function droppedLine(decision: BreakDecision): string | null {
  const labels: Record<string, string> = {
    below_bar: "below the tier's bar",
    ad_load: "over the tier's ad cap",
  };
  const parts = Object.entries(decision.dropped)
    .filter(([, n]) => n > 0)
    .map(([outcome, n]) => `${n} ${labels[outcome] ?? outcome.replace(/_/g, " ")}`);
  return parts.length > 0 ? `Not run: ${parts.join(", ")}` : null;
}
