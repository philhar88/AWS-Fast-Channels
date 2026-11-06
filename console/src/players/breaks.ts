/**
 * What a viewer sees of an ad break: the stitched ads one by one, then the channel's slate for the seconds no ad
 * filled. MediaTailor stitches each ad behind its own discontinuity, and the playlists keep Channel Assembly's
 * `EXT-X-DATERANGE` with `SCTE35-OUT` and the break's `DURATION`. So a playing fragment is an ad when its URL is an ad
 * segment's (players/telemetry.ts), slate when it lies inside the break's date range without being one, and content
 * otherwise. Each change of discontinuity sequence inside the ads is the next ad.
 */
import type Hls from "hls.js";
import { Events } from "hls.js";
import { isAdSegment } from "./telemetry";

export type BreakPhase =
  | { readonly kind: "content" }
  | { readonly kind: "ad"; readonly index: number }
  | { readonly kind: "slate" };

/** One break as it played: how many ads, and how many seconds of ads and of slate. */
export interface PlayedBreak {
  readonly ads: number;
  readonly adSeconds: number;
  readonly slateSeconds: number;
}

/** The parts of an hls.js fragment the tracker reads. */
export interface PlayedFragment {
  readonly url: string;
  /** Discontinuity sequence number. */
  readonly cc: number;
  /** Seconds. */
  readonly duration: number;
  /** Epoch milliseconds from `EXT-X-PROGRAM-DATE-TIME`, when the playlist has it. */
  readonly programDateTime: number | null;
}

/** The parts of an hls.js `DateRange` the tracker reads. */
export interface BreakRange {
  readonly startDate: Date;
  readonly duration: number | null;
  readonly plannedDuration: number | null;
  readonly attr: Readonly<Record<string, unknown>>;
}

/** Whether `time` (epoch ms) lies inside an ad break's date range (`SCTE35-OUT`, with a duration). */
export function inBreakRange(time: number | null, ranges: Iterable<BreakRange | undefined>): boolean {
  if (time === null) {
    return false;
  }
  for (const range of ranges) {
    const seconds = range?.duration ?? range?.plannedDuration;
    if (range && seconds && range.attr["SCTE35-OUT"] !== undefined) {
      const start = range.startDate.getTime();
      if (start <= time && time < start + seconds * 1000) {
        return true;
      }
    }
  }
  return false;
}

/** Follows the playing fragment through breaks; feed it every fragment change. */
export class BreakTracker {
  private phase: BreakPhase = { kind: "content" };
  private cc = -1;
  private ads = 0;
  private adSeconds = 0;
  private slateSeconds = 0;

  /** `page` resolves a relative `cdnBaseUrl` (mock mode); the current page by default. */
  constructor(
    private readonly cdnBaseUrl: string,
    private readonly page = window.location.href,
  ) {}

  /** The phase of `fragment`, and the break that just ended when it returns to content. */
  observe(fragment: PlayedFragment, ranges: Iterable<BreakRange | undefined>): { phase: BreakPhase; ended?: PlayedBreak } {
    if (isAdSegment(fragment.url, this.cdnBaseUrl, this.page)) {
      if (this.phase.kind !== "ad" || fragment.cc !== this.cc) {
        this.ads += 1;
      }
      this.cc = fragment.cc;
      this.adSeconds += fragment.duration;
      this.phase = { kind: "ad", index: this.ads };
      return { phase: this.phase };
    }
    if (inBreakRange(fragment.programDateTime, ranges)) {
      this.slateSeconds += fragment.duration;
      this.phase = { kind: "slate" };
      return { phase: this.phase };
    }
    const wasInBreak = this.phase.kind !== "content";
    const ended = wasInBreak ? { ads: this.ads, adSeconds: this.adSeconds, slateSeconds: this.slateSeconds } : undefined;
    this.phase = { kind: "content" };
    this.cc = -1;
    this.ads = 0;
    this.adSeconds = 0;
    this.slateSeconds = 0;
    return ended ? { phase: this.phase, ended } : { phase: this.phase };
  }
}

export interface BreakSink {
  phase(phase: BreakPhase): void;
  played(played: PlayedBreak): void;
}

/** Reports the break phase of the playing fragment, and every break once it ends. */
export function attachBreakTelemetry(hls: Hls, sink: BreakSink, cdnBaseUrl: string): void {
  const tracker = new BreakTracker(cdnBaseUrl);
  let last: BreakPhase["kind"] | number = "content";
  hls.on(Events.FRAG_CHANGED, (_event, { frag }) => {
    const ranges = Object.values(hls.levels[frag.level]?.details?.dateRanges ?? {});
    const { phase, ended } = tracker.observe(frag, ranges);
    const key = phase.kind === "ad" ? phase.index : phase.kind;
    if (key !== last) {
      last = key;
      sink.phase(phase);
    }
    if (ended) {
      sink.played(ended);
    }
  });
}
