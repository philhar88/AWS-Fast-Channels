/**
 * Typed wrappers for the Gateway tools the console calls directly. The tool registry is the source of truth
 * (`fast_channels.functions.tools`); `tool-schemas.json` is generated from it and the contract test checks these
 * types and the mock fixtures against it. Only the fields the console renders are declared. Results never carry
 * `null`: the tools drop empty optional fields, so those are optional here.
 */
import type { ChannelPlayback } from "./config";
import type { Gateway } from "./gateway";

/** Live channel state; MISSING: in the media library but not in Channel Assembly. */
export type ChannelState = "RUNNING" | "STOPPED" | "MISSING";

/** `iac`: defined by the deployment; `agent`: created with the channels tools; `pipeline`: encoded by the ingest pipeline. */
export type Owner = "iac" | "pipeline" | "agent";

/** `channels.ChannelSummary` (channels___list_channels). */
export interface ChannelSummary {
  readonly name: string;
  readonly state: ChannelState;
  readonly owner: Exclude<Owner, "pipeline">;
  readonly ladder_version: string;
  readonly outputs: readonly string[];
  readonly tier?: string;
}

/** `channels.CdnPlayback`: the channel's viewer URLs through one CDN. */
export interface CdnPlayback {
  readonly cdn: string;
  readonly playback: ChannelPlayback;
}

/** `channels.ChannelDetail` (channels___get_channel). */
export interface ChannelDetail extends ChannelSummary {
  /** Through the primary CDN; the shape of `config.json` channels[].playback (`core.specs.playback_urls`). */
  readonly playback: ChannelPlayback;
  /** Through every configured CDN, primary first; only when the deployment lists other CDNs (`delivery.cdns`). */
  readonly cdn_playback?: readonly CdnPlayback[];
  /** When the scheduled auto-stop fires (`channels.max_runtime_hours`). */
  readonly auto_stop_at?: string;
}

/** `_alerts.Alert`: a Channel Assembly alert, expected warnings left out. */
export interface ChannelAlert {
  readonly code: string;
  readonly category: string;
  readonly message: string;
}

/** `channels.StateChange` (channels___start_channel / channels___stop_channel). */
export interface ChannelStateChange {
  readonly channel: string;
  readonly state: "RUNNING" | "STOPPED";
  /** When the scheduled auto-stop fires (`channels.max_runtime_hours`). */
  readonly auto_stop_at?: string;
  /** SCHEDULING_ERROR alerts break playback. */
  readonly alerts: readonly ChannelAlert[];
}

export type AssetKind = "content" | "slate" | "ad";
export type AssetStatus = "INGESTING" | "ENCODED" | "READY" | "FAILED";

/** `library.AssetSummary`: one compact library row (library___find_assets). */
export interface Asset {
  readonly asset_id: string;
  readonly kind: AssetKind;
  readonly ladder_version: string;
  readonly status: AssetStatus;
  readonly owner: Owner;
  readonly title?: string;
  readonly content_id?: string;
  readonly duration_ms?: number;
  /** How many valid ad-break offsets the asset has (library___get_asset lists them). */
  readonly break_offset_count: number;
  readonly vod_source?: string;
  readonly updated_at: string;
}

interface AssetPage {
  readonly assets: readonly Asset[];
  readonly next_token?: string;
}

export type LadderStatus = "PROPOSED" | "PROVISIONING" | "READY" | "FAILED";

/** `encoding.KitSummary`: the slates and house ads encoded on a ladder version. */
export interface LadderKit {
  readonly filler?: string;
  readonly break_slates: readonly string[];
  readonly house_ads: readonly string[];
}

/** `encoding.LadderSummary` (encoding___list_ladders). */
export interface Ladder {
  readonly ladder_version: string;
  readonly status: LadderStatus;
  readonly owner: Owner;
  readonly description?: string;
  readonly codecs?: readonly string[];
  readonly max_height?: number;
  readonly renditions?: number;
  readonly cost_per_minute_usd?: number;
  readonly kit: LadderKit;
}

export interface AssetQuery {
  readonly kind?: AssetKind;
  readonly ladder_version?: string;
  readonly limit?: number;
}

/** Pages library___find_assets reads at most (100 assets per page). */
export const MAX_ASSET_PAGES = 10;

export const listChannels = async (gateway: Gateway): Promise<readonly ChannelSummary[]> =>
  (await gateway.call<{ channels: ChannelSummary[] }>("channels___list_channels")).channels;

export const getChannel = (gateway: Gateway, channel: string): Promise<ChannelDetail> =>
  gateway.call<ChannelDetail>("channels___get_channel", { channel });

/** CONFIRM tool: the operator's click in the confirmation dialog is the approval. */
export const startChannel = (gateway: Gateway, channel: string): Promise<ChannelStateChange> =>
  gateway.call<ChannelStateChange>("channels___start_channel", { channel });

/** CONFIRM tool: the operator's click in the confirmation dialog is the approval. */
export const stopChannel = (gateway: Gateway, channel: string): Promise<ChannelStateChange> =>
  gateway.call<ChannelStateChange>("channels___stop_channel", { channel });

/** Every asset matching `query`, following `next_token` for up to {@link MAX_ASSET_PAGES} pages. */
export async function findAssets(gateway: Gateway, query: AssetQuery = {}): Promise<readonly Asset[]> {
  const assets: Asset[] = [];
  let token: string | undefined;
  for (let page = 0; page < MAX_ASSET_PAGES; page++) {
    const result = await gateway.call<AssetPage>("library___find_assets", { ...query, ...(token ? { next_token: token } : {}) });
    assets.push(...result.assets);
    token = result.next_token;
    if (!token) {
      break;
    }
  }
  return assets;
}

export const listLadders = async (gateway: Gateway): Promise<readonly Ladder[]> =>
  (await gateway.call<{ ladders: Ladder[] }>("encoding___list_ladders")).ladders;
