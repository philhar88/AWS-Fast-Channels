import { describe, expect, it } from "vitest";
import { showCaptions } from "./telemetry";

const session = (tracks: number) => ({
  subtitleDisplay: false,
  subtitleTrack: -1,
  subtitleTracks: Array.from({ length: tracks }, (_, id) => ({ id })) as never,
});

describe("showCaptions", () => {
  it("shows the first caption track", () => {
    const hls = session(2);
    showCaptions(hls, true);
    expect([hls.subtitleDisplay, hls.subtitleTrack]).toEqual([true, 0]);
  });

  it("hides captions", () => {
    const hls = session(1);
    showCaptions(hls, true);
    showCaptions(hls, false);
    expect([hls.subtitleDisplay, hls.subtitleTrack]).toEqual([false, -1]);
  });

  it("selects nothing when the stream has no caption tracks", () => {
    const hls = session(0);
    showCaptions(hls, true);
    expect(hls.subtitleTrack).toBe(-1);
  });
});
