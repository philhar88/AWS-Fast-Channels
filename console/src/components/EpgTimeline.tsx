import {
  borderRadiusBadge,
  colorBackgroundContainerHeader,
  colorBackgroundStatusInfo,
  colorBorderDividerDefault,
  colorBorderStatusInfo,
  colorBorderStatusWarning,
  colorChartsStatusHigh,
  colorTextBodySecondary,
} from "@cloudscape-design/design-tokens";
import type { CSSProperties } from "react";
import { formatClock } from "../lib/format";
import { GUIDE_PAST_MS, guideStatus, type EpgProgram } from "../lib/schedule";
import styles from "./EpgTimeline.module.css";

const BEFORE_MS = GUIDE_PAST_MS;
const AFTER_MS = 3.5 * 3_600_000;
const TICK_MS = 30 * 60_000;

/** Design tokens as custom properties, set through the CSSOM so the strict CSP needs no 'unsafe-inline'. */
const THEME = {
  "--epg-block": colorBackgroundContainerHeader,
  "--epg-border": colorBorderDividerDefault,
  "--epg-on-air": colorBackgroundStatusInfo,
  "--epg-on-air-border": colorBorderStatusInfo,
  "--epg-break": colorChartsStatusHigh,
  "--epg-skipped": colorBorderStatusWarning,
  "--epg-muted": colorTextBodySecondary,
  "--epg-radius": borderRadiusBadge,
} as CSSProperties;

const CLASSES: Partial<Record<ReturnType<typeof guideStatus>, string>> = {
  ON_AIR: `${styles.program} ${styles.onAir}`,
  SKIPPED: `${styles.program} ${styles.skipped}`,
};

const LABELS: Partial<Record<ReturnType<typeof guideStatus>, string>> = { ON_AIR: " · on air", SKIPPED: " · skipped" };

interface EpgTimelineProps {
  readonly programs: readonly EpgProgram[];
  readonly now: number;
  /** The channel is running: the program at `now` is highlighted as on air. */
  readonly onAir: boolean;
}

/**
 * Four hours of the program guide around `now`: program blocks, their ad breaks, and a now marker. Skipped
 * programs are hatched: filler slate played in their slot.
 */
export function EpgTimeline({ programs, now, onAir }: EpgTimelineProps) {
  const start = now - BEFORE_MS;
  const end = now + AFTER_MS;
  const percent = (time: number) => ((Math.min(Math.max(time, start), end) - start) / (end - start)) * 100;
  const ticks = Array.from({ length: Math.floor((end - start) / TICK_MS) + 1 }, (_, i) => Math.ceil(start / TICK_MS) * TICK_MS + i * TICK_MS).filter((t) => t <= end);
  const visible = programs.filter((p) => Date.parse(p.end) > start && Date.parse(p.start) < end);

  return (
    <div className={styles.timeline} style={THEME}>
      <div className={styles.axis} aria-hidden="true">
        {ticks.map((tick) => (
          <span key={tick} className={styles.tick} style={{ insetInlineStart: `${percent(tick)}%` }}>
            {formatClock(tick)}
          </span>
        ))}
      </div>
      <ol className={styles.track} aria-label="Program timeline">
        {visible.map((program) => {
          const programStart = Date.parse(program.start);
          const programEnd = Date.parse(program.end);
          const status = guideStatus(program, onAir ? now : undefined);
          return (
            <li
              key={program.name}
              className={CLASSES[status] ?? styles.program}
              style={{ insetInlineStart: `${percent(programStart)}%`, inlineSize: `${percent(programEnd) - percent(programStart)}%` }}
              title={`${program.title}, ${formatClock(programStart)} to ${formatClock(programEnd)}${LABELS[status] ?? ""}`}
            >
              <span className={styles.title}>{program.title}</span>
              <span className={styles.time}>
                {formatClock(programStart)}
                {LABELS[status]}
              </span>
              {program.breaks.map((b) => (
                <span
                  key={b.offset_ms}
                  className={styles.break}
                  style={{ insetInlineStart: `${(b.offset_ms / (programEnd - programStart)) * 100}%` }}
                  aria-hidden="true"
                />
              ))}
            </li>
          );
        })}
      </ol>
      <div className={styles.now} style={{ insetInlineStart: `${percent(now)}%` }} aria-hidden="true" />
    </div>
  );
}
