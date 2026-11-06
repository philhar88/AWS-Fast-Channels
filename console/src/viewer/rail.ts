/**
 * The recommended-channels rail, rule-based (DESIGN §15's genre baseline): a persona's declared genres, most liked
 * first, against the genres and titles of what each channel airs, from the public EPG feeds (`epg/<channel>.json`).
 * No viewer data leaves the page and no model is called; every tile carries the reasons it was ranked by.
 *
 * - A channel scores the persona's affinity with its program on now, then half of the next one's and a quarter of
 *   the best later one's within {@link HORIZON_MS}. Affinity is the sum of the matched genres' weights, which fall
 *   with the genre's rank (1, 2/3, 1/2, 2/5, …).
 * - A channel in an ad break now is ranked down (its score × {@link IN_BREAK_FACTOR}): a viewer sent there would
 *   start on ads. The rail re-ranks on every render, so the break phase is always current.
 * - Without declared genres (guests), channels rank by whether they air something now, then by name.
 */
import type { Epg, EpgProgram } from "../lib/schedule";
import type { ViewerChannel } from "./config";

export const HORIZON_MS = 3 * 3_600_000;
export const IN_BREAK_FACTOR = 0.75;

export interface ChannelGuide {
  readonly channel: ViewerChannel;
  /** `null` while the channel has no published guide. */
  readonly epg: Epg | null;
}

export interface ChannelTile {
  readonly channel: ViewerChannel;
  readonly score: number;
  readonly now?: EpgProgram;
  readonly next?: EpgProgram;
  /** The on-air end of the break playing now (epoch ms), if one is. */
  readonly breakEndsAt?: number;
  /** Why the tile is where it is, for its "why" line. */
  readonly reasons: readonly string[];
}

export interface ProgramTile {
  readonly channel: ViewerChannel;
  readonly program: EpgProgram;
  readonly onNow: boolean;
  readonly score: number;
  readonly matched: readonly string[];
}

/** Each declared genre's weight: 1 for the first, falling with rank. */
export function genreWeights(genres: readonly string[]): Map<string, number> {
  return new Map(genres.map((genre, rank) => [genre.toLowerCase(), 1 / (1 + rank / 2)]));
}

/** The persona's affinity with `program`, and the genres that matched, in the persona's order. */
export function affinity(program: EpgProgram | undefined, weights: Map<string, number>): { score: number; matched: string[] } {
  const genres = new Set(program?.genres.map((g) => g.toLowerCase()) ?? []);
  const matched = [...weights.keys()].filter((genre) => genres.has(genre));
  return { score: matched.reduce((total, genre) => total + (weights.get(genre) ?? 0), 0), matched };
}

/** On-air windows of `program`'s breaks: each break's content offset plus the breaks before it. */
export function breakWindows(program: EpgProgram): { start: number; end: number }[] {
  const start = Date.parse(program.start);
  let aired = 0;
  return [...program.breaks]
    .sort((a, b) => a.offset_ms - b.offset_ms)
    .map((b) => {
      const at = start + b.offset_ms + aired;
      aired += b.duration_ms;
      return { start: at, end: at + b.duration_ms };
    });
}

const airs = (program: EpgProgram) => program.status !== "SKIPPED";

function nowAndNext(programs: readonly EpgProgram[], now: number): { current?: EpgProgram; next?: EpgProgram; later: EpgProgram[] } {
  const live = programs.filter(airs);
  const current = live.find((p) => Date.parse(p.start) <= now && now < Date.parse(p.end));
  const upcoming = live.filter((p) => Date.parse(p.start) > now && Date.parse(p.start) < now + HORIZON_MS);
  const [next, ...later] = upcoming;
  return { current, next, later };
}

const clock = (time: number | string) =>
  new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(new Date(time));

const secondsLeft = (ms: number) => {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

/** The channels, best first, for a persona who declared `genres`. */
export function channelRail(genres: readonly string[], guides: readonly ChannelGuide[], now: number): ChannelTile[] {
  const weights = genreWeights(genres);
  const tiles = guides.map((guide): ChannelTile => {
    const { current, next, later } = nowAndNext(guide.epg?.programs ?? [], now);
    const onNow = affinity(current, weights);
    const upNext = affinity(next, weights);
    const best = Math.max(0, ...later.map((p) => affinity(p, weights).score));
    const reasons: string[] = [];
    let score: number;
    if (weights.size === 0) {
      score = current ? 1 : 0;
      reasons.push(current ? "On now" : "Nothing on now");
    } else {
      score = onNow.score + 0.5 * upNext.score + 0.25 * best;
      if (onNow.matched.length > 0 && current) {
        reasons.push(`You like ${onNow.matched.join(" and ")}: ${current.title} is on now`);
      }
      if (upNext.matched.length > 0 && next) {
        reasons.push(`${next.title} at ${clock(next.start)} (${upNext.matched.join(", ")})`);
      }
      if (reasons.length === 0) {
        reasons.push(best > 0 ? "More of your genres later today" : "Outside your genres");
      }
    }
    const window = current && breakWindows(current).find((w) => w.start <= now && now < w.end);
    if (window) {
      score *= IN_BREAK_FACTOR;
      reasons.push(`In an ad break, back in ${secondsLeft(window.end - now)}: ranked down`);
    }
    return {
      channel: guide.channel,
      score,
      ...(current ? { now: current } : {}),
      ...(next ? { next } : {}),
      ...(window ? { breakEndsAt: window.end } : {}),
      reasons,
    };
  });
  return tiles.sort((a, b) => b.score - a.score || a.channel.name.localeCompare(b.channel.name));
}

/**
 * Programs on now or starting within {@link HORIZON_MS} that match `genres`, best match first, then soonest: each
 * title once, at its first airing in that order.
 */
export function programRail(genres: readonly string[], guides: readonly ChannelGuide[], now: number, limit = 8): ProgramTile[] {
  const weights = genreWeights(genres);
  const tiles = guides.flatMap((guide) =>
    (guide.epg?.programs ?? [])
      .filter((p) => airs(p) && Date.parse(p.end) > now && Date.parse(p.start) < now + HORIZON_MS)
      .map((program): ProgramTile => {
        const { score, matched } = affinity(program, weights);
        return { channel: guide.channel, program, onNow: Date.parse(program.start) <= now, score, matched };
      }),
  );
  const seen = new Set<string>();
  return tiles
    .filter((tile) => weights.size === 0 || tile.score > 0)
    .sort((a, b) => b.score - a.score || Date.parse(a.program.start) - Date.parse(b.program.start))
    .filter((tile) => {
      const title = tile.program.title.toLowerCase();
      return !seen.has(title) && Boolean(seen.add(title));
    })
    .slice(0, limit);
}
