/**
 * The viewer app's `config.json`, written next to its index.html at deploy time by the viewer module
 * (`fast_channels.infra.constructs.viewer.ViewerConfig`). It names the viewer pool's client, the playback API, the
 * media CDN, the channels and the demo personas. It holds no operator endpoint.
 */

/** How a persona watches: a viewer-pool user, a kids profile on another user's account, or a guest. */
export type SignIn = "user" | "kids_profile" | "guest";

/** A demo persona's public fields (`contracts/personas.json`). Tiers and tolerances are not here, by design. */
export interface Persona {
  readonly id: string;
  readonly name: string;
  readonly bio: string;
  /** Sign-in username; `null` for a guest and a kids profile. */
  readonly email: string | null;
  readonly signIn: SignIn;
  /** A kids profile's account: the persona whose sign-in it uses; `null` otherwise. */
  readonly account?: string | null;
  /** Declared genres, most liked first, in the catalog's genre vocabulary: the rail's seed. */
  readonly genres: readonly string[];
}

export interface ViewerChannel {
  readonly name: string;
  /** A channel with monetized playback configurations: it plays on a ticket from `POST /playback`. */
  readonly monetized: boolean;
  /** Output name (`hls`) → the anonymous SSAI URL through the CDN (v3.0.0's, unmonetized). */
  readonly playback: Readonly<Record<string, string>>;
}

export interface ViewerConfig {
  readonly region: string;
  /** The viewer pool's public client (USER_PASSWORD_AUTH): its access tokens carry `fast/playback`. */
  readonly userPoolClientId: string;
  /** `https://<api>.execute-api.<region>.amazonaws.com/<stage>/playback`: `POST` here mints a ticket. */
  readonly playbackUrl: string;
  readonly cdnBaseUrl: string;
  readonly epgBaseUrl: string;
  /** `<cdn>/plan/playback`: each monetized channel's anonymous URL, the fallback when no ticket can be minted. */
  readonly planBaseUrl: string;
  /** The demo's "why these ads" API, or `null` when `modules.viewer.ad_panel` is off. */
  readonly adPanelUrl: string | null;
  readonly channels: readonly ViewerChannel[];
  readonly personas: readonly Persona[];
}

const REQUIRED_STRINGS = [
  "region",
  "userPoolClientId",
  "playbackUrl",
  "cdnBaseUrl",
  "epgBaseUrl",
  "planBaseUrl",
] as const satisfies readonly (keyof ViewerConfig)[];

/** Checks the fields the app cannot work without; throws an Error naming every missing one. */
export function parseViewerConfig(raw: unknown): ViewerConfig {
  if (typeof raw !== "object" || raw === null) {
    throw new Error("config.json is not a JSON object");
  }
  const record = raw as Record<string, unknown>;
  const missing: string[] = REQUIRED_STRINGS.filter((key) => typeof record[key] !== "string" || record[key] === "");
  for (const key of ["channels", "personas"] as const) {
    if (!Array.isArray(record[key])) {
      missing.push(key);
    }
  }
  if (!(typeof record.adPanelUrl === "string" || record.adPanelUrl === null)) {
    missing.push("adPanelUrl");
  }
  if (missing.length > 0) {
    throw new Error(`config.json is missing ${missing.join(", ")}`);
  }
  return raw as ViewerConfig;
}

/** Fetches and validates `/config.json`; never cached, so a redeploy takes effect on the next page load. */
export async function loadViewerConfig(url = "/config.json"): Promise<ViewerConfig> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${url} returned HTTP ${response.status}`);
  }
  return parseViewerConfig(await response.json());
}
