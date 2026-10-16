import type { ChannelState } from "../lib/tools";

/** A channel that cannot serve a stream: every manifest request fails (504 while stopped, 404 when missing). */
export type OffAirState = Exclude<ChannelState, "RUNNING">;

/**
 * What the Channels view shows where the players go:
 * - `play`: open hls.js sessions;
 * - `off-air`: the channel is STOPPED or MISSING;
 * - `checking`: the first state request is still in flight.
 */
export type PlayerGate =
  | { readonly kind: "play" }
  | { readonly kind: "off-air"; readonly state: OffAirState }
  | { readonly kind: "checking" };

/**
 * Sessions open only for a channel that is, or may be, on air. When the Gateway cannot tell (`stateUnavailable`
 * with no state known), the players try anyway: the media path does not depend on the Gateway, and they back off
 * while the stream fails.
 */
export function playerGate(state: ChannelState | undefined, stateUnavailable: boolean): PlayerGate {
  switch (state) {
    case "RUNNING":
      return { kind: "play" };
    case "STOPPED":
    case "MISSING":
      return { kind: "off-air", state };
    case undefined:
      return { kind: stateUnavailable ? "play" : "checking" };
  }
}
