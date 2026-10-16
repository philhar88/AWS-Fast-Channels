/** Fixture data for mock mode: one deployment with two channels, a small library and a rolling EPG. */
import type { AppConfig, OutputPlayback } from "../lib/config";
import type { Epg, EpgBreak, EpgProgram, Plan, PlannedProgram } from "../lib/schedule";
import type { Asset, ChannelDetail, Ladder } from "../lib/tools";

/** Served by the `fast-mock-cdn` Vite plugin from `mock/cdn` (see scripts/mock-media.sh). */
const CDN = new URL("/mock/cdn", window.location.href).href;
const LADDER = "standard@3f9c2a71d0";
const BREAK_MS = 60_060; // 30 GOPs of 2.002 s: the BREAK_SLATE_GOPS default
const GOP_MS = 2_002;

const hls: OutputPlayback = {
  ssai: `${CDN}/channel/ssai.m3u8`,
  sgai: `${CDN}/channel/sgai.m3u8?aws.insertionMode=GUIDED`,
};

export const MOCK_CONFIG: AppConfig = {
  region: "us-east-1",
  userPoolId: "us-east-1_EXAMPLE",
  userPoolClientId: "example-client-id",
  cognitoDomain: "https://fast-channels-example.auth.us-east-1.amazoncognito.com",
  runtimeArn: "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/fast_channels_scheduler-EXAMPLE",
  runtimeInvokeUrl: "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/EXAMPLE/invocations?qualifier=DEFAULT",
  gatewayUrl: "https://fast-channels-example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
  cdnBaseUrl: CDN,
  epgBaseUrl: `${CDN}/epg`,
  channels: [
    {
      name: "fast-demo",
      ladderVersion: LADDER,
      playback: { hls, "hls-avc": hls, "hls-hevc": hls, "hls-av1": hls, "dash-avc": { dash: `${CDN}/channel/dash-avc.mpd` } },
    },
    { name: "fast-kids", ladderVersion: LADDER, playback: { hls, "hls-avc": hls } },
  ],
};

interface Title {
  readonly content_id: string;
  readonly title: string;
  readonly duration_ms: number;
  readonly rating: string;
  readonly genres: readonly string[];
}

/** Blender open movies (CC BY), the sample catalog's titles. */
export const CATALOG: readonly Title[] = [
  { content_id: "bbb", title: "Big Buck Bunny", duration_ms: 596_462, rating: "TV-G", genres: ["animation", "comedy"] },
  { content_id: "sintel", title: "Sintel", duration_ms: 888_032, rating: "TV-14", genres: ["animation", "fantasy"] },
  { content_id: "tos", title: "Tears of Steel", duration_ms: 734_167, rating: "TV-14", genres: ["sci-fi"] },
  { content_id: "ed", title: "Elephants Dream", duration_ms: 653_791, rating: "TV-PG", genres: ["animation", "sci-fi"] },
  { content_id: "spring", title: "Spring", duration_ms: 464_464, rating: "TV-G", genres: ["animation", "fantasy"] },
  { content_id: "cosmos", title: "Cosmos Laundromat", duration_ms: 730_063, rating: "TV-14", genres: ["animation", "drama"] },
  { content_id: "sprite", title: "Sprite Fright", duration_ms: 629_128, rating: "TV-PG", genres: ["animation", "comedy"] },
  { content_id: "coffee", title: "Coffee Run", duration_ms: 184_183, rating: "TV-G", genres: ["animation", "comedy"] },
];

const assetId = (contentId: string) => `${contentId}-${contentId.length.toString(16).padStart(2, "0")}a3f1`;
const breakOffsets = (duration: number) => [300_300, 600_600].filter((offset) => offset < duration - 60_000);

export const ASSETS: readonly Asset[] = [
  ...CATALOG.map(
    (item): Asset => ({
      asset_id: assetId(item.content_id),
      kind: "content",
      ladder_version: LADDER,
      status: "READY",
      title: item.title,
      content_id: item.content_id,
      duration_ms: item.duration_ms,
      break_offsets_ms: breakOffsets(item.duration_ms),
      vod_source: `fast-${assetId(item.content_id)}`,
      updated_at: "2026-10-15T21:14:03Z",
    }),
  ),
  ...[15, 30, 45, 60].map(
    (gops): Asset => ({
      asset_id: `slate-${gops}g`,
      kind: "slate",
      ladder_version: LADDER,
      status: "READY",
      title: `Break slate, ${gops} GOPs`,
      duration_ms: gops * GOP_MS,
      vod_source: `fast-slate-${gops}g`,
      updated_at: "2026-10-15T20:02:41Z",
    }),
  ),
  ...[7, 15].map(
    (gops): Asset => ({
      asset_id: `house-ad-${gops}g`,
      kind: "ad",
      ladder_version: LADDER,
      status: "READY",
      title: `House ad, ${gops} GOPs`,
      duration_ms: gops * GOP_MS,
      updated_at: "2026-10-15T20:05:12Z",
    }),
  ),
  {
    asset_id: "agent-327-5c1d9e",
    kind: "content",
    ladder_version: LADDER,
    status: "INGESTING",
    title: "Agent 327: Operation Barbershop",
    content_id: "agent327",
    updated_at: "2026-10-16T11:52:37Z",
  },
  {
    asset_id: "hdr-trailer-0b7731",
    kind: "content",
    ladder_version: LADDER,
    status: "FAILED",
    title: "HDR trailer (refused: HDR source into an SDR ladder)",
    updated_at: "2026-10-16T09:31:08Z",
  },
];

export const LADDERS: readonly Ladder[] = [
  {
    ladder_version: LADDER,
    status: "READY",
    description: "AVC 8 + HEVC 4 + AV1 4 + AAC 3, capped at 1080p (default)",
    codecs: ["avc", "hevc", "av1"],
    renditions: 16,
    max_height: 1080,
    cost_per_minute_usd: 0.56,
    kit: { filler: "slate-30g", break_slates: ["slate-15g", "slate-30g", "slate-45g", "slate-60g"], house_ads: ["house-ad-7g", "house-ad-15g"] },
  },
  {
    ladder_version: "uhd@8b1e44c2f7",
    status: "PROVISIONING",
    description: "AVC 8 + HEVC Main10 7 + AV1 10-bit 7 + AAC 4, up to 2160p",
    codecs: ["avc", "hevc", "av1"],
    renditions: 22,
    max_height: 2160,
    cost_per_minute_usd: 1.09,
    kit: { filler: null, break_slates: ["slate-15g"], house_ads: [] },
  },
  {
    ladder_version: "standard-av1@51d0e8b3aa",
    status: "PROPOSED",
    description: "Agent proposal: AV1 rungs only, capped at 1080p",
    codecs: ["av1"],
    renditions: 4,
    max_height: 1080,
    cost_per_minute_usd: 0.21,
  },
];

export const CHANNELS: readonly ChannelDetail[] = MOCK_CONFIG.channels.map((channel) => ({
  name: channel.name,
  state: channel.name === "fast-demo" ? "RUNNING" : "STOPPED",
  ladder_version: channel.ladderVersion,
  outputs: Object.keys(channel.playback),
  owner: "iac",
}));

const programName = (startMs: number, asset: string) =>
  `${new Date(startMs).toISOString().replaceAll(/[-:]/g, "").slice(0, 15)}-${asset}`;

function epgProgram(item: Title, startMs: number, now: number): EpgProgram {
  const breaks: EpgBreak[] = breakOffsets(item.duration_ms).map((offset_ms) => ({ offset_ms, duration_ms: BREAK_MS }));
  const endMs = startMs + item.duration_ms + breaks.length * BREAK_MS;
  return {
    name: programName(startMs, assetId(item.content_id)),
    title: item.title,
    content_id: item.content_id,
    asset_id: assetId(item.content_id),
    start: new Date(startMs).toISOString(),
    end: new Date(endMs).toISOString(),
    rating: item.rating,
    genres: item.genres,
    breaks,
    status: endMs < now ? "AIRED" : "PUBLISHED",
  };
}

/** A schedule from the top of the previous hour to four hours ahead, back to back. */
export function mockEpg(channel: string, now = Date.now()): Epg {
  const hour = 3_600_000;
  const programs: EpgProgram[] = [];
  let start = Math.floor(now / hour) * hour - hour;
  for (let i = channel === "fast-kids" ? 4 : 0; start < now + 4 * hour; i++) {
    const item = CATALOG[i % CATALOG.length];
    if (!item) {
      break;
    }
    const program = epgProgram(item, start, now);
    programs.push(program);
    start = Date.parse(program.end);
  }
  return { channel, generated_at: new Date(now).toISOString(), ladder_version: LADDER, programs };
}

/** The plan the mock agent proposes: family titles appended after the published schedule. */
export function mockPlan(channel: string, now = Date.now()): Plan {
  const family = CATALOG.filter((item) => item.rating !== "TV-14");
  let start = Date.parse(mockEpg(channel, now).programs.at(-1)?.end ?? new Date(now).toISOString());
  const programs = [...family, ...family.slice(0, 1)].map((item, index): PlannedProgram => {
    const program: PlannedProgram = {
      name: programName(start, assetId(item.content_id)),
      asset_id: assetId(item.content_id),
      vod_source: `fast-${assetId(item.content_id)}`,
      content_id: item.content_id,
      start_ms: start,
      duration_ms: item.duration_ms + breakOffsets(item.duration_ms).length * BREAK_MS,
      breaks: breakOffsets(item.duration_ms).map((offset_ms, breakIndex) => ({
        offset_ms,
        slate_gops: 30,
        event_id: 0x3c00 + index * 16 + breakIndex,
        upid: `FST3${item.content_id}`,
      })),
      ladder_version: LADDER,
    };
    start += program.duration_ms;
    return program;
  });
  const startMs = programs[0]?.start_ms ?? now;
  return {
    plan_id: "plan-7d41c0e2",
    channel,
    ladder_version: LADDER,
    programs,
    violations: [
      {
        code: "REPEAT",
        severity: "warning",
        message: "Big Buck Bunny airs twice within 2 hours.",
        program: programs.at(-1)?.name ?? null,
      },
    ],
    start_ms: startMs,
    end_ms: start,
  };
}
