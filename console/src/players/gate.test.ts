import { describe, expect, it } from "vitest";
import type { ChannelState } from "../lib/tools";
import { playerGate, type PlayerGate } from "./gate";

describe("playerGate", () => {
  it.each<[ChannelState | undefined, boolean, PlayerGate]>([
    ["RUNNING", false, { kind: "play" }],
    ["STOPPED", false, { kind: "off-air", state: "STOPPED" }],
    ["MISSING", false, { kind: "off-air", state: "MISSING" }],
    [undefined, false, { kind: "checking" }],
    // The Gateway failed before it answered once: try the stream, the player backs off while it fails.
    [undefined, true, { kind: "play" }],
    // A failed poll keeps the last state the Gateway reported.
    ["STOPPED", true, { kind: "off-air", state: "STOPPED" }],
    ["RUNNING", true, { kind: "play" }],
  ])("%s (state unavailable: %s)", (state, unavailable, expected) => {
    expect(playerGate(state, unavailable)).toEqual(expected);
  });
});
