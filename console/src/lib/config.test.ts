import { describe, expect, it } from "vitest";
import { parseConfig } from "./config";

describe("parseConfig", () => {
  it("names every missing field", () => {
    expect(() => parseConfig({ region: "us-east-1", gatewayUrl: "" })).toThrow(
      "config.json is missing userPoolId, userPoolClientId, cognitoDomain, runtimeInvokeUrl, gatewayUrl, cdnBaseUrl, epgBaseUrl, channels",
    );
    expect(() => parseConfig(null)).toThrow("not a JSON object");
  });
});
