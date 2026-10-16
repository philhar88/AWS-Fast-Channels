import Spinner from "@cloudscape-design/components/spinner";
import type { OffAirState, PlayerGate } from "../players/gate";
import styles from "./OffAirScreen.module.css";

const STATE_LINES: Record<OffAirState, readonly [state: string, hint: string]> = {
  STOPPED: ["is stopped", "The players start as soon as the channel runs."],
  // In the media library but not in Channel Assembly.
  MISSING: ["is missing from Channel Assembly", "It was deleted outside the deployment."],
};

/** Stands in for a player, at its size, while the channel cannot serve a stream; no hls.js session behind it. */
export function OffAirScreen({ channel, gate }: { channel: string; gate: Exclude<PlayerGate, { kind: "play" }> }) {
  if (gate.kind === "checking") {
    return (
      <div className={styles.screen} role="status">
        <Spinner size="big" variant="inverted" />
        <span className={styles.detail}>Checking channel state</span>
      </div>
    );
  }
  const [state, hint] = STATE_LINES[gate.state];
  return (
    <div className={styles.screen} role="status">
      <span className={styles.title}>Off air</span>
      <span className={styles.detail}>
        {channel} {state}
      </span>
      <span className={styles.hint}>{hint}</span>
    </div>
  );
}
