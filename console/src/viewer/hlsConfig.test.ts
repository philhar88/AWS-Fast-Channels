import { describe, expect, it } from "vitest";
import { VIEWER_HLS_CONFIG } from "./hlsConfig";

describe("the viewer's hls.js options", () => {
  it("send CMCD in query mode and load a ticketed URL once", () => {
    expect(VIEWER_HLS_CONFIG.cmcd).toEqual({});
    expect("useHeaders" in VIEWER_HLS_CONFIG.cmcd).toBe(false); // headers would preflight every request
    expect(VIEWER_HLS_CONFIG.manifestLoadPolicy.default).toMatchObject({ timeoutRetry: null, errorRetry: null });
    expect(VIEWER_HLS_CONFIG.enableInterstitialPlayback).toBe(false);
  });
});
