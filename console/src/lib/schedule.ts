/**
 * Schedule shapes shared by the agent's STATE_SNAPSHOT (the schedule___plan result, and guides built from the
 * schedule___get_schedule, publish and remove_programs results, CONTRACTS §G) and the EPG feed (CONTRACTS §I).
 * Checked against `tool-schemas.json` by the contract test.
 */
import { formatDuration } from "./format";

/** `fast_channels.core.timeline.PlannedBreak`: an ad break `offset_ms` into the content. */
export interface PlannedBreak {
  readonly offset_ms: number;
  readonly duration_ms: number;
  readonly slate_gops: number;
  readonly event_id: number;
  readonly upid: string;
}

/** `schedule.PlannedRow`: a core `PlannedProgram` with its UTC times and catalog metadata. */
export interface PlannedProgram {
  readonly name: string;
  readonly asset_id: string;
  readonly vod_source: string;
  readonly content_id?: string;
  readonly start_ms: number;
  readonly start: string;
  readonly end: string;
  /** On-air duration: content plus ad breaks. */
  readonly duration_ms: number;
  readonly breaks: readonly PlannedBreak[];
  readonly ladder_version: string;
  readonly title: string;
  readonly rating?: string;
  readonly genres: readonly string[];
}

/** `fast_channels.core.timeline.Violation`: errors block publishing, warnings do not. */
export interface Violation {
  readonly code: string;
  readonly severity: "error" | "warning";
  readonly message: string;
  /** The program (or picked asset id) the violation is about. */
  readonly program?: string;
}

/** `schedule.PlanOutput`: the schedule___plan result. */
export interface Plan {
  readonly plan_id: string;
  readonly channel: string;
  readonly ladder_version: string;
  readonly start_ms: number;
  readonly start: string;
  readonly end_ms: number;
  readonly end: string;
  readonly programs: readonly PlannedProgram[];
  readonly violations: readonly Violation[];
  /** False while any violation has error severity. */
  readonly publishable: boolean;
  /** The plan can be published until this UTC time. */
  readonly expires_at: string;
}

export interface EpgBreak {
  readonly offset_ms: number;
  readonly duration_ms: number;
}

/**
 * `epg.ProgramStatus`. PLANNED and PUBLISHED: in the schedule; AIRED: in the as-run log; SKIPPED: never aired,
 * because the channel started after the program's planned start (channels.start_channel removes it from Channel
 * Assembly rather than let it push the rest of the schedule late).
 */
export const PROGRAM_STATUSES = ["PLANNED", "PUBLISHED", "AIRED", "SKIPPED"] as const;
export type ProgramStatus = (typeof PROGRAM_STATUSES)[number];

/** `epg.EpgProgram`. The feed file writes `null` for a missing content id or rating; tool results omit them. */
export interface EpgProgram {
  readonly name: string;
  readonly title: string;
  readonly content_id?: string | null;
  readonly asset_id: string;
  readonly start: string;
  readonly end: string;
  readonly rating?: string | null;
  readonly genres: readonly string[];
  readonly breaks: readonly EpgBreak[];
  readonly status: ProgramStatus;
}

/** The past the program guide shows: the timeline's first half hour, and how long it lists a skipped program. */
export const GUIDE_PAST_MS = 30 * 60_000;

/**
 * What the guide shows for `program`: ON_AIR while it runs at `onAirAt`, else its status. Pass `onAirAt` only
 * while the channel is running; a skipped program is never on air, filler slate plays in its place.
 */
export function guideStatus(program: EpgProgram, onAirAt: number | undefined): ProgramStatus | "ON_AIR" {
  const airing = onAirAt !== undefined && Date.parse(program.start) <= onAirAt && onAirAt < Date.parse(program.end);
  return airing && program.status !== "SKIPPED" ? "ON_AIR" : program.status;
}

/**
 * The guide's rows at `now`: every program that has not ended, and the programs skipped in the last
 * {@link GUIDE_PAST_MS}, which an operator who just started the channel needs to see.
 */
export function guidePrograms(programs: readonly EpgProgram[], now: number): EpgProgram[] {
  return programs.filter((p) => {
    const end = Date.parse(p.end);
    return end > now || (p.status === "SKIPPED" && end > now - GUIDE_PAST_MS);
  });
}

/** `2 × 1:00` when a program's breaks are equally long, else their count and total length: `3, 2:30 in all`. */
export function breakSummary(breaks: readonly EpgBreak[]): string {
  const durations = new Set(breaks.map((b) => b.duration_ms));
  const [only] = durations;
  if (only === undefined) {
    return "None";
  }
  if (durations.size === 1) {
    return `${breaks.length} × ${formatDuration(only)}`;
  }
  return `${breaks.length}, ${formatDuration(breaks.reduce((total, b) => total + b.duration_ms, 0))} in all`;
}

/** `epg/<channel>.json` on the media CDN; also the core of the schedule___publish and get_schedule results. */
export interface Epg {
  readonly channel: string;
  readonly generated_at: string;
  readonly ladder_version: string;
  readonly programs: readonly EpgProgram[];
}

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
