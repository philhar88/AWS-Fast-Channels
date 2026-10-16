/** Schedule shapes shared by the agent (STATE_SNAPSHOT, CONTRACTS §G) and the EPG feed (CONTRACTS §I). */

/** `fast_channels.core.timeline.PlannedBreak`. */
export interface PlannedBreak {
  readonly offset_ms: number;
  readonly slate_gops: number;
  readonly event_id: number;
  readonly upid: string;
}

/** `fast_channels.core.timeline.PlannedProgram`. */
export interface PlannedProgram {
  readonly name: string;
  readonly asset_id: string;
  readonly vod_source: string;
  readonly content_id: string | null;
  readonly start_ms: number;
  readonly duration_ms: number;
  readonly breaks: readonly PlannedBreak[];
  readonly ladder_version: string;
}

/** `fast_channels.core.timeline.Violation`. */
export interface Violation {
  readonly code: string;
  readonly severity: "error" | "warning";
  readonly message: string;
  readonly program: string | null;
}

/** `fast_channels.core.timeline.Plan` as serialised by `model_dump(mode="json")`; `plan_id` when the tool adds it. */
export interface Plan {
  readonly channel: string;
  readonly ladder_version: string;
  readonly programs: readonly PlannedProgram[];
  readonly violations: readonly Violation[];
  readonly start_ms: number;
  readonly end_ms: number;
  readonly plan_id?: string;
}

export interface EpgBreak {
  readonly offset_ms: number;
  readonly duration_ms: number;
}

export interface EpgProgram {
  readonly name: string;
  readonly title: string;
  readonly content_id: string | null;
  readonly asset_id: string;
  readonly start: string;
  readonly end: string;
  readonly rating: string | null;
  readonly genres: readonly string[];
  readonly breaks: readonly EpgBreak[];
  readonly status: "PLANNED" | "PUBLISHED" | "AIRED";
}

/** `epg/<channel>.json` on the media CDN. */
export interface Epg {
  readonly channel: string;
  readonly generated_at: string;
  readonly ladder_version: string;
  readonly programs: readonly EpgProgram[];
}

/** True when the plan has no error-severity violations (mirrors `Plan.ok` in core). */
export const planIsPublishable = (plan: Plan): boolean => plan.violations.every((v) => v.severity !== "error");

/**
 * Fetches a channel's EPG from `<epgBaseUrl>/<channel>.json`. A channel that has never been published has no feed
 * yet, so 403/404 (S3 behind OAC answers 403 for a missing key) resolve to `null` rather than an error.
 */
export async function fetchEpg(epgBaseUrl: string, channel: string, signal?: AbortSignal): Promise<Epg | null> {
  const response = await fetch(`${epgBaseUrl}/${encodeURIComponent(channel)}.json`, { cache: "no-cache", signal });
  if (response.status === 403 || response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`EPG for ${channel} returned HTTP ${response.status}`);
  }
  return (await response.json()) as Epg;
}
