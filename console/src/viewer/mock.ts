/**
 * Mock mode of the viewer (`?mock`): the demo personas, two channels with a rolling guide of the sample catalog's
 * titles, a sign-in that accepts any password but `wrong`, and per-persona pods on the local mock CDN (scripts/mock-media.sh).
 * The tiers here are fixtures of the mock "why these ads" answers only; the deployed app never knows a tier.
 */
import type { Epg, EpgProgram } from "../lib/schedule";
import type { BreakDecision, SessionDecisions } from "./adPanel";
import type { Tokens } from "./auth";
import { SignInError } from "./auth";
import type { ViewerBackend } from "./backend";
import type { Persona, ViewerChannel, ViewerConfig } from "./config";
import type { ConsentRecord } from "./consent";
import type { Source } from "./playback";

const CDN = new URL("/mock/cdn", window.location.href).href;
const STANDARD_POD = `${CDN}/channel/hls.m3u8`;
const LIGHT_POD = `${CDN}/channel/hls-light.m3u8`;

export const MOCK_PERSONAS: readonly Persona[] = [
  { id: "dana", name: "Dana Reyes", bio: "Designer in Austin; binges sci-fi in prime time.", email: "dana@personas.example.com", signIn: "user", genres: ["sci-fi", "action", "fantasy"] },
  { id: "ben", name: "Ben Okafor", bio: "Dana's twin, pinned to the holdout.", email: "ben@personas.example.com", signIn: "user", genres: ["sci-fi", "action", "fantasy"] },
  { id: "marcus", name: "Marcus Hale", bio: "Retired engineer; watches live action every evening.", email: "marcus@personas.example.com", signIn: "user", genres: ["live-action", "action"] },
  { id: "luis", name: "Luis Moreno", bio: "Freelancer; animation in the background all day.", email: "luis@personas.example.com", signIn: "user", genres: ["animation", "surreal"] },
  { id: "priya", name: "Priya Shah", bio: "Nurse in San Jose; an hour of fantasy after a shift.", email: "priya@personas.example.com", signIn: "user", genres: ["fantasy", "adventure"] },
  { id: "sam", name: "Sam Lee", bio: "Student, new this week; declared animation and sci-fi.", email: "sam@personas.example.com", signIn: "user", genres: ["animation", "sci-fi"] },
  { id: "mia", name: "Mia", bio: "8; a kids profile on Marcus's account.", email: null, signIn: "kids_profile", account: "marcus", genres: ["animation"] },
  { id: "guest", name: "Guest", bio: "A drive-by viewer who never signs in.", email: null, signIn: "guest", genres: [] },
];

const TIERS: Readonly<Record<string, string>> = { dana: "light", ben: "holdout" };

const CHANNELS: readonly ViewerChannel[] = [
  { name: "fast-demo", monetized: true, playback: { hls: STANDARD_POD } },
  { name: "fast-anim", monetized: false, playback: { hls: STANDARD_POD } },
];

export const MOCK_VIEWER_CONFIG: ViewerConfig = {
  region: "us-east-1",
  userPoolClientId: "example-client-id",
  playbackUrl: "https://api.example/v1/playback",
  cdnBaseUrl: CDN,
  epgBaseUrl: `${CDN}/epg`,
  planBaseUrl: `${CDN}/plan/playback`,
  adPanelUrl: "https://panel.example/",
  channels: CHANNELS,
  personas: MOCK_PERSONAS,
};

interface Title {
  readonly id: string;
  readonly title: string;
  readonly minutes: number;
  readonly genres: readonly string[];
}

/** The sample catalog (assets/sample-catalog.json). */
const TITLES: Readonly<Record<string, Title>> = {
  sintel: { id: "sintel", title: "Sintel", minutes: 16, genres: ["animation", "fantasy", "adventure"] },
  tos: { id: "tears-of-steel", title: "Tears of Steel", minutes: 14, genres: ["sci-fi", "action", "live-action"] },
  ed: { id: "elephants-dream", title: "Elephants Dream", minutes: 12, genres: ["animation", "surreal"] },
};
// fast-demo airs only Tears of Steel, so the mock rails never depend on the time of day.
const LINEUP: Readonly<Record<string, readonly string[]>> = { "fast-demo": ["tos"], "fast-anim": ["ed", "sintel"] };

/** A guide from an hour ago to three hours ahead, the channel's lineup back to back, with one minute break each. */
export function mockViewerEpg(channel: string, now = Date.now()): Epg {
  const lineup = LINEUP[channel] ?? [];
  const programs: EpgProgram[] = [];
  let start = Math.floor(now / 3_600_000) * 3_600_000 - 3_600_000;
  for (let i = 0; lineup.length > 0 && start < now + 3 * 3_600_000; i++) {
    const item = TITLES[lineup[i % lineup.length] ?? ""];
    if (!item) {
      break;
    }
    const end = start + (item.minutes + 1) * 60_000;
    programs.push({
      name: `${new Date(start).toISOString()}-${item.id}`,
      title: item.title,
      content_id: item.id,
      asset_id: item.id,
      start: new Date(start).toISOString(),
      end: new Date(end).toISOString(),
      rating: "TV-PG",
      genres: item.genres,
      breaks: [{ offset_ms: 5 * 60_000, duration_ms: 60_000 }],
      status: end < now ? "AIRED" : "PUBLISHED",
    });
    start = end;
  }
  return { channel, generated_at: new Date(now).toISOString(), ladder_version: "standard@3f9c2a71d0", programs };
}

const base64url = (value: unknown) =>
  btoa(JSON.stringify(value)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function mockTokens(persona: Persona): Tokens {
  return {
    accessToken: `mock-access-${persona.id}`,
    idToken: ["e30", base64url({ sub: `sub-${persona.id}`, email: persona.email, "custom:persona": persona.id }), "mock"].join("."),
    refreshToken: `mock-refresh-${persona.id}`,
    expiresAt: Date.now() + 3_600_000,
  };
}

const personaOfToken = (token: string) => MOCK_PERSONAS.find((p) => token === `mock-access-${p.id}`);
const sessionId = (id: string) => `00000000-0000-4000-8000-${id.padEnd(12, "0").slice(0, 12).replace(/[^0-9a-f]/g, "0")}`;

function mockDecisions(persona: Persona | undefined, sid: string): SessionDecisions {
  const tier = persona ? (TIERS[persona.id] ?? "standard") : null;
  const light = tier === "light";
  const at = new Date(Date.now() - 60_000).toISOString();
  const decision: BreakDecision = {
    decision_id: `${sid}.101`,
    evid: "101",
    at,
    mode: persona ? "FULL" : "CONTEXTUAL",
    mode_reason: persona ? "bound" : "missing",
    tier,
    ad_load: light
      ? { policy: "light", reason: "tier", max_ads: 1, bar_cpm: 20, slots: 2 }
      : { policy: "standard", reason: tier === null ? "exempt" : "control", max_ads: null, bar_cpm: 0, slots: 2 },
    pod_reason: light ? "partial" : "filled",
    ads: [
      { bidder: "amt", price_cpm: 35, house: false, deal: null, seconds: 30.03 },
      ...(light ? [] : [{ bidder: "amt", price_cpm: 22.5, house: false, deal: null, seconds: 30.03 }]),
    ],
    filled_s: light ? 30.03 : 60.06,
    break_s: 60.06,
    dropped: light ? { below_bar: 4, ad_load: 1 } : { not_needed: 3 },
  };
  return { sid, breaks: [decision] };
}

export function mockViewerBackend(): ViewerBackend {
  let mints = 0;
  const consents = new Map<string, ConsentRecord>();
  return {
    config: MOCK_VIEWER_CONFIG,
    signIn: (username, password) => {
      const persona = MOCK_PERSONAS.find((p) => p.email === username);
      if (!persona || password === "" || password === "wrong") {
        return Promise.reject(new SignInError("Incorrect username or password.", "NotAuthorizedException"));
      }
      return Promise.resolve(mockTokens(persona));
    },
    refresh: (refreshToken) => {
      const persona = MOCK_PERSONAS.find((p) => refreshToken === `mock-refresh-${p.id}`);
      return persona ? Promise.resolve(mockTokens(persona)) : Promise.reject(new Error("unknown refresh token"));
    },
    open: async (channel, accessToken, _signal, profile): Promise<Source> => {
      const persona = accessToken ? personaOfToken(await accessToken()) : undefined;
      if (profile === "kids") {
        return { url: STANDARD_POD, kids: true, sid: sessionId("kids") };
      }
      if (!channel.monetized) {
        return { url: STANDARD_POD, anonymous: "not monetized", sid: sessionId("guest") };
      }
      mints += 1;
      if (!persona) {
        return { url: STANDARD_POD, cid: `01MOCKGUEST${String(mints).padStart(4, "0")}`, sid: sessionId("guest") };
      }
      const url = TIERS[persona.id] === "light" ? LIGHT_POD : STANDARD_POD;
      return { url, cid: `01MOCKCID${String(mints).padStart(4, "0")}`, sid: sessionId(persona.id) };
    },
    consent: (accessToken) => Promise.resolve(consents.get(personaOfToken(accessToken)?.id ?? "") ?? null),
    setConsent: (accessToken, choice) => {
      const record = { opt_out: choice.opt_out, vppa: choice.vppa, updated_at: Date.now() };
      consents.set(personaOfToken(accessToken)?.id ?? "", record);
      return Promise.resolve(record);
    },
    epg: (channel) => Promise.resolve(mockViewerEpg(channel)),
    decisions: (sid, accessToken) => Promise.resolve(mockDecisions(personaOfToken(accessToken), sid)),
  };
}
