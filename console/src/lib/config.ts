/** Playback URLs of one channel output (CONTRACTS §H; built by `fast_channels.core.specs.playback_urls`). */
export interface OutputPlayback {
  readonly ssai?: string;
  /** The SSAI URL plus `?aws.insertionMode=GUIDED`. */
  readonly sgai?: string;
}

/** Output (package profile) name, `hls` (the one HLS MPEG-TS output), mapped to its playback URLs. */
export type ChannelPlayback = Readonly<Record<string, OutputPlayback>>;

export interface ChannelConfig {
  readonly name: string;
  readonly ladderVersion: string;
  readonly playback: ChannelPlayback;
}

/** `config.json`, written next to index.html at deploy time (CONTRACTS §H). */
export interface AppConfig {
  readonly region: string;
  readonly userPoolId: string;
  readonly userPoolClientId: string;
  readonly cognitoDomain: string;
  readonly runtimeArn: string;
  readonly runtimeInvokeUrl: string;
  readonly gatewayUrl: string;
  readonly cdnBaseUrl: string;
  readonly epgBaseUrl: string;
  readonly channels: readonly ChannelConfig[];
}

const REQUIRED_STRINGS = [
  "region",
  "userPoolId",
  "userPoolClientId",
  "cognitoDomain",
  "runtimeInvokeUrl",
  "gatewayUrl",
  "cdnBaseUrl",
  "epgBaseUrl",
] as const satisfies readonly (keyof AppConfig)[];

/** Checks the fields the console cannot work without; throws an Error naming every missing one. */
export function parseConfig(raw: unknown): AppConfig {
  if (typeof raw !== "object" || raw === null) {
    throw new Error("config.json is not a JSON object");
  }
  const record = raw as Record<string, unknown>;
  const missing: string[] = REQUIRED_STRINGS.filter((key) => typeof record[key] !== "string" || record[key] === "");
  if (!Array.isArray(record.channels)) {
    missing.push("channels");
  }
  if (missing.length > 0) {
    throw new Error(`config.json is missing ${missing.join(", ")}`);
  }
  return raw as AppConfig;
}

/** Fetches and validates `/config.json`; never cached, so a redeploy takes effect on the next page load. */
export async function loadConfig(url = "/config.json"): Promise<AppConfig> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${url} returned HTTP ${response.status}`);
  }
  return parseConfig(await response.json());
}

/** Mock mode: `VITE_MOCK=1` at build/dev time, or `?mock` on the page URL. */
export function isMockMode(): boolean {
  return import.meta.env.VITE_MOCK === "1" || new URLSearchParams(window.location.search).has("mock");
}
