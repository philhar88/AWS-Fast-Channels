import { describe, expect, it } from "vitest";
import { isAdSegment } from "./telemetry";

const page = "https://console.example/";

describe("isAdSegment", () => {
  it.each([
    ["https://d1.cloudfront.net/v1/segment/h/cfg/session/1/2", true],
    ["https://d1.cloudfront.net/tm/h/transcode/asset_1080_0_1.ts", true],
    ["https://d1.cloudfront.net/ads/standard_3f9c/cr-a/main_avc-720p_00001.ts", true],
    ["https://d1.cloudfront.net/assets/ads-special/standard/main_avc-720p_00001.ts", false],
  ])("%s is an ad: %s", (url, expected) => {
    expect(isAdSegment(url, "https://d1.cloudfront.net", page)).toBe(expected);
  });

  it("matches paths relative to a CDN root below the origin", () => {
    expect(isAdSegment("/mock/cdn/ads/cr-a/000.ts", "/mock/cdn", page)).toBe(true);
    expect(isAdSegment("/mock/cdn/assets/program/000.ts", "/mock/cdn", page)).toBe(false);
  });
});
