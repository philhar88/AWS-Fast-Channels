/**
 * The recommender's "Recommended for you" rail (`GET /rail`, the recs module; ADR-0027). The answer says how the rail
 * was made and why: the viewer's model rail (`llm`), the hourly cohort rail (`cohort`), the rule-based rail
 * (`baseline`) or the kids lineup (`kids`), each tile with the reason it is there. The app shows that label, and its
 * own rule-based rail (rail.ts) when the request fails.
 */

export type RailStrategy = "llm" | "cohort" | "baseline" | "kids";

export interface RailTile {
  readonly content_id: string;
  readonly title: string;
  readonly channel: string;
  /** The Channel Assembly program the title airs as (its EPG `name`). */
  readonly program: string;
  readonly start: string;
  readonly end: string;
  readonly on_now: boolean;
  /** The model's reason, or its evidence's template where the validator replaced it. */
  readonly reason: string;
  readonly evidence: readonly string[];
}

export interface RailLineage {
  readonly model: string;
  readonly prompt_version: string;
  readonly built_at: string;
}

export interface RailAnswer {
  readonly strategy: RailStrategy;
  /** Why this rail: `model`, `first_session`, `holdout`, `cap`, `building`, … */
  readonly reason: string;
  readonly title: string;
  readonly tiles: readonly RailTile[];
  readonly lineage: RailLineage | null;
}

export class RailError extends Error {
  override readonly name = "RailError";
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** `GET <railUrl>` with the viewer's access token; `query` carries `profile=kids` or the device. */
export async function fetchRail(
  railUrl: string,
  accessToken: string,
  query: Readonly<Record<string, string>> = {},
  signal?: AbortSignal,
): Promise<RailAnswer> {
  const url = new URL(railUrl);
  for (const [key, value] of Object.entries(query)) {
    url.searchParams.set(key, value);
  }
  const response = await fetch(url, { headers: { Authorization: `Bearer ${accessToken}` }, cache: "no-store", signal });
  if (!response.ok) {
    throw new RailError(`GET /rail returned HTTP ${response.status}`, response.status);
  }
  return (await response.json()) as RailAnswer;
}

const REASONS: Readonly<Record<string, string>> = {
  model: "picked and explained by the recommender",
  first_session: "your first session gets the audience rail",
  no_taste: "no taste facts yet",
  opted_out: "you opted out of personalization",
  no_consent: "no privacy choices recorded",
  holdout: "you are in the rule-based holdout",
  kill_switch: "the recommender is switched off",
  building: "your rail is being built",
  expired: "your rail is being rebuilt",
  cap: "the recommender's daily cap is reached",
  timeout: "the recommender timed out",
  model_error: "the recommender failed",
  invalid: "the recommender's rail did not pass the checks",
  unavailable: "the audience rail is not built yet",
  kids: "the kids lineup",
};

/** A one-line description of how a rail was made, for the rail's header. */
export function describeRail(answer: RailAnswer): string {
  const why = REASONS[answer.reason] ?? answer.reason;
  switch (answer.strategy) {
    case "llm":
      return `Recommended by ${modelName(answer.lineage?.model)}: ${why}.`;
    case "cohort":
      return `Picked for this audience (no personal data): ${why}.`;
    case "baseline":
      return `Rule-based rail from your genres: ${why}.`;
    case "kids":
      return "The kids lineup: no recommender, no personal data.";
  }
}

function modelName(model: string | undefined): string {
  if (!model) {
    return "the recommender";
  }
  const family = model.replace(/^(global|us|eu|apac|ca|jp|au)\./, "").replace(/^anthropic\.claude-/, "");
  return family.startsWith("haiku-5-5") ? "Claude Haiku 5.5" : family;
}
