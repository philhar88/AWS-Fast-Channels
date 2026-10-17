/** Fixture data for mock mode: one deployment with two channels, a small library and a rolling EPG. */
import type { AppConfig, ChannelPlayback, OutputPlayback } from "../lib/config";
import type { Epg, EpgBreak, EpgProgram, Plan, PlannedProgram } from "../lib/schedule";
import type { Asset, ChannelDetail, ChannelSummary, Ladder } from "../lib/tools";

/** Served by the `fast-mock-cdn` Vite plugin from `mock/cdn` (see scripts/mock-media.sh). */
const CDN = new URL("/mock/cdn", window.location.href).href;
const LADDER = "standard@3f9c2a71d0";
const BREAK_MS = 60_060; // 30 GOPs of 2.002 s: the BREAK_SLATE_GOPS default
const GOP_MS = 2_002;
const SEGMENT_MS = 6_006; // every segment boundary common to all renditions is a valid break offset

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
    { name: "fast-demo", ladderVersion: LADDER, playback: { hls } },
    { name: "fast-kids", ladderVersion: LADDER, playback: { hls } },
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
/** Editorial break positions the plans use, on the segment grid. */
const breakOffsets = (duration: number) => [300_300, 600_600].filter((offset) => offset < duration - 60_000);

export const ASSETS: readonly Asset[] = [
  ...CATALOG.map(
    (item): Asset => ({
      asset_id: assetId(item.content_id),
      kind: "content",
      ladder_version: LADDER,
      status: "READY",
      owner: "pipeline",
      title: item.title,
      content_id: item.content_id,
      duration_ms: item.duration_ms,
      break_offset_count: Math.floor(item.duration_ms / SEGMENT_MS),
      vod_source: `${assetId(item.content_id)}-standard_3f9c2a71d0`,
      updated_at: "2026-10-15T21:14:03.000Z",
    }),
  ),
  ...[15, 30, 45, 60].map(
    (gops): Asset => ({
      asset_id: `slate-${gops}g`,
      kind: "slate",
      ladder_version: LADDER,
      status: "READY",
      owner: "pipeline",
      title: `Break slate, ${gops} GOPs`,
      duration_ms: gops * GOP_MS,
      break_offset_count: 0,
      vod_source: `slate-${gops}g-standard_3f9c2a71d0`,
      updated_at: "2026-10-15T20:02:41.000Z",
    }),
  ),
  ...[7, 15].map(
    (gops): Asset => ({
      asset_id: `house-ad-${gops}g`,
      kind: "ad",
      ladder_version: LADDER,
      status: "READY",
      owner: "pipeline",
      title: `House ad, ${gops} GOPs`,
      duration_ms: gops * GOP_MS,
      break_offset_count: 0,
      updated_at: "2026-10-15T20:05:12.000Z",
    }),
  ),
  {
    asset_id: "agent-327-5c1d9e",
    kind: "content",
    ladder_version: LADDER,
    status: "INGESTING",
    owner: "pipeline",
    title: "Agent 327: Operation Barbershop",
    content_id: "agent327",
    break_offset_count: 0,
    updated_at: "2026-10-16T11:52:37.000Z",
  },
  {
    asset_id: "hdr-trailer-0b7731",
    kind: "content",
    ladder_version: LADDER,
    status: "FAILED",
    owner: "pipeline",
    title: "HDR trailer (refused: HDR source into an SDR ladder)",
    break_offset_count: 0,
    updated_at: "2026-10-16T09:31:08.000Z",
  },
];

export const LADDERS: readonly Ladder[] = [
  {
    ladder_version: LADDER,
    status: "READY",
    owner: "iac",
    description: "AVC + AAC-LC muxed in MPEG-TS, up to 1080p, 29.97 fps",
    renditions: 3,
    max_height: 1080,
    cost_per_minute_usd: 0.0375,
    kit: {
      filler: "slate-30g-standard_3f9c2a71d0",
      break_slates: [15, 30, 45, 60].map((gops) => `slate-${gops}g-standard_3f9c2a71d0`),
      house_ads: ["house-ad-7g-standard_3f9c2a71d0", "house-ad-15g-standard_3f9c2a71d0"],
    },
  },
  {
    ladder_version: "standard-hq@8b1e44c2f7",
    status: "PROVISIONING",
    owner: "agent",
    description: "derived from standard@3f9c2a71d0, QVBR +1",
    renditions: 3,
    max_height: 1080,
    cost_per_minute_usd: 0.0375,
    kit: { break_slates: ["slate-15g-standard-hq_8b1e44c2f7"], house_ads: [] },
  },
  {
    ladder_version: "standard-720p@51d0e8b3aa",
    status: "PROPOSED",
    owner: "agent",
    description: "derived from standard@3f9c2a71d0, without avc-1080p",
    renditions: 2,
    max_height: 720,
    cost_per_minute_usd: 0.0225,
    kit: { break_slates: [], house_ads: [] },
  },
];

/** The mock deployment's other CDN (`delivery.cdns`): same paths, another host. Listed, never fetched. */
function viaOtherCdn(playback: ChannelPlayback): ChannelPlayback {
  const move = (url: string) => url.replace(CDN, "https://cdn-b.example.net");
  return Object.fromEntries(
    Object.entries(playback).map(([output, { ssai, sgai }]): [string, OutputPlayback] => [
      output,
      { ...(ssai ? { ssai: move(ssai) } : {}), ...(sgai ? { sgai: move(sgai) } : {}) },
    ]),
  );
}

export const CHANNELS: readonly ChannelDetail[] = MOCK_CONFIG.channels.map((channel) => ({
  name: channel.name,
  state: channel.name === "fast-demo" ? "RUNNING" : "STOPPED",
  owner: "iac",
  ladder_version: channel.ladderVersion,
  outputs: Object.keys(channel.playback),
  tier: "BASIC",
  playback: channel.playback,
  cdn_playback: [
    { cdn: "cloudfront", playback: channel.playback },
    { cdn: "cdn-b", playback: viaOtherCdn(channel.playback) },
  ],
}));

/** A list_channels row: the summary fields of a channel. */
export const summary = ({ name, state, owner, ladder_version, outputs, tier }: ChannelDetail): ChannelSummary => ({
  name,
  state,
  owner,
  ladder_version,
  outputs,
  ...(tier === undefined ? {} : { tier }),
});

const programName = (startMs: number, asset: string) =>
  `${new Date(startMs).toISOString().replaceAll(/[-:]/g, "").slice(0, 15)}-${asset}`;

/**
 * fast-demo was started this long ago, after its schedule had begun. Starting a channel skips every program planned
 * to start before then (channels.start_channel), so the in-progress program and the ones before it are SKIPPED.
 */
const STARTED_AGO_MS: Readonly<Record<string, number>> = { "fast-demo": 20 * 60_000 };

function epgProgram(item: Title, startMs: number, now: number, startedAt: number, skipped: ReadonlySet<string>): EpgProgram {
  const breaks: EpgBreak[] = breakOffsets(item.duration_ms).map((offset_ms) => ({ offset_ms, duration_ms: BREAK_MS }));
  const endMs = startMs + item.duration_ms + breaks.length * BREAK_MS;
  const name = programName(startMs, assetId(item.content_id));
  return {
    name,
    title: item.title,
    content_id: item.content_id,
    asset_id: assetId(item.content_id),
    start: new Date(startMs).toISOString(),
    end: new Date(endMs).toISOString(),
    rating: item.rating,
    genres: item.genres,
    breaks,
    status: startMs < startedAt || skipped.has(name) ? "SKIPPED" : endMs < now ? "AIRED" : "PUBLISHED",
  };
}

/**
 * A schedule from the top of the previous hour to four hours ahead, back to back. `skipped`: programs a start in
 * this mock session skipped (mockGateway).
 */
export function mockEpg(channel: string, now = Date.now(), skipped: ReadonlySet<string> = new Set()): Epg {
  const hour = 3_600_000;
  const startedAt = now - (STARTED_AGO_MS[channel] ?? Infinity);
  const programs: EpgProgram[] = [];
  let start = Math.floor(now / hour) * hour - hour;
  for (let i = channel === "fast-kids" ? 4 : 0; start < now + 4 * hour; i++) {
    const item = CATALOG[i % CATALOG.length];
    if (!item) {
      break;
    }
    const program = epgProgram(item, start, now, startedAt, skipped);
    programs.push(program);
    start = Date.parse(program.end);
  }
  return { channel, generated_at: new Date(now).toISOString(), ladder_version: LADDER, programs };
}

const FILLER_BEFORE_PLAN_MS = 120_000;

/** The schedule___plan result the mock agent gets: family titles two minutes after the published schedule. */
export function mockPlan(channel: string, now = Date.now()): Plan {
  const family = CATALOG.filter((item) => item.rating !== "TV-14");
  const scheduleEnd = Date.parse(mockEpg(channel, now).programs.at(-1)?.end ?? new Date(now).toISOString());
  let start = scheduleEnd + FILLER_BEFORE_PLAN_MS;
  const programs = family.map((item, index): PlannedProgram => {
    const offsets = breakOffsets(item.duration_ms);
    const program: PlannedProgram = {
      name: programName(start, assetId(item.content_id)),
      asset_id: assetId(item.content_id),
      vod_source: `${assetId(item.content_id)}-standard_3f9c2a71d0`,
      content_id: item.content_id,
      start_ms: start,
      start: new Date(start).toISOString(),
      end: new Date(start + item.duration_ms + offsets.length * BREAK_MS).toISOString(),
      duration_ms: item.duration_ms + offsets.length * BREAK_MS,
      breaks: offsets.map((offset_ms, breakIndex) => ({
        offset_ms,
        duration_ms: BREAK_MS,
        slate_gops: 30,
        event_id: 0x3c00 + index * 16 + breakIndex,
        upid: `FST3${item.content_id}`,
      })),
      ladder_version: LADDER,
      title: item.title,
      rating: item.rating,
      genres: item.genres,
    };
    start += program.duration_ms;
    return program;
  });
  const startMs = scheduleEnd + FILLER_BEFORE_PLAN_MS;
  return {
    plan_id: `${channel}.7d41c0e2`,
    channel,
    ladder_version: LADDER,
    start_ms: startMs,
    start: new Date(startMs).toISOString(),
    end_ms: start,
    end: new Date(start).toISOString(),
    programs,
    violations: [{ code: "GAP", severity: "warning", message: `${FILLER_BEFORE_PLAN_MS / 1000} s of filler slate before the block` }],
    publishable: true,
    expires_at: new Date(now + 3_600_000).toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
}

/** The schedule___publish result for `plan`: the published block as guide entries. */
export function mockPublished(plan: Plan, now = Date.now()) {
  return {
    channel: plan.channel,
    generated_at: new Date(now).toISOString(),
    ladder_version: plan.ladder_version,
    programs: plan.programs.map(
      (p): EpgProgram => ({
        name: p.name,
        title: p.title,
        ...(p.content_id === undefined ? {} : { content_id: p.content_id }),
        asset_id: p.asset_id,
        start: p.start,
        end: p.end,
        ...(p.rating === undefined ? {} : { rating: p.rating }),
        genres: p.genres,
        breaks: p.breaks.map(({ offset_ms, duration_ms }) => ({ offset_ms, duration_ms })),
        status: "PUBLISHED",
      }),
    ),
    plan_id: plan.plan_id,
    published: plan.programs.length,
    start_ms: plan.start_ms,
    start: plan.start,
    end_ms: plan.end_ms,
    end: plan.end,
    alerts: [],
  };
}

/** The schedule___get_schedule result: the guide from now on, where it ends and its gaps. */
export function mockSchedule(channel: string, now = Date.now()) {
  const epg = mockEpg(channel, now);
  const upcoming = epg.programs.filter((p) => Date.parse(p.end) > now);
  const last = upcoming.at(-1);
  return { ...epg, programs: upcoming, ...(last ? { end_ms: Date.parse(last.end), end: last.end } : {}), gaps: [] };
}
