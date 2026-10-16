/**
 * Typed wrappers for the Gateway tools the console calls directly (the tool registry is the source of truth:
 * `fast_channels.functions.tools`). Only the fields the console renders are declared; the rest pass through.
 */
import type { ChannelPlayback } from "./config";
import type { Gateway } from "./gateway";

/** MediaTailor `ChannelState`. */
export type ChannelState = "RUNNING" | "STOPPED";

export interface ChannelSummary {
  readonly name: string;
  readonly state: ChannelState;
  readonly ladder_version?: string;
  readonly outputs?: readonly string[];
  readonly owner?: "iac" | "agent";
}

export interface ChannelDetail extends ChannelSummary {
  /** Same shape as `config.json` channels[].playback (`core.specs.playback_urls`); set for agent-created channels. */
  readonly playback?: ChannelPlayback;
}

export interface ChannelStateChange {
  readonly channel: string;
  readonly state: ChannelState;
  /** When the scheduled auto-stop fires (`channels.max_runtime_hours`). */
  readonly auto_stop_at?: string;
}

export type AssetKind = "content" | "slate" | "ad";
export type AssetStatus = "INGESTING" | "ENCODED" | "READY" | "FAILED";

/** A `MediaLibrary` asset item (`ASSET#<id>` / `LADDER#<ladder_version>`). */
export interface Asset {
  readonly asset_id: string;
  readonly kind: AssetKind;
  readonly ladder_version: string;
  readonly status: AssetStatus;
  readonly title?: string;
  readonly content_id?: string | null;
  readonly duration_ms?: number | null;
  readonly break_offsets_ms?: readonly number[];
  readonly vod_source?: string | null;
  readonly updated_at?: string;
}

export type LadderStatus = "PROPOSED" | "PROVISIONING" | "READY" | "FAILED";

/** A `MediaLibrary` ladder version item (`LADDER#<id>` / `V#<ladder_version>`) and its kit. */
export interface Ladder {
  readonly ladder_version: string;
  readonly status: LadderStatus;
  readonly description?: string;
  readonly codecs?: readonly string[];
  readonly max_height?: number;
  readonly renditions?: number;
  readonly cost_per_minute_usd?: number;
  readonly kit?: {
    readonly filler?: string | null;
    readonly break_slates?: readonly string[];
    readonly house_ads?: readonly string[];
  };
}

export interface AssetQuery {
  readonly kind?: AssetKind;
  readonly ladder_version?: string;
  readonly limit?: number;
}

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

export const findAssets = async (gateway: Gateway, query: AssetQuery = {}): Promise<readonly Asset[]> =>
  (await gateway.call<{ assets: Asset[] }>("library___find_assets", { ...query })).assets;

export const listLadders = async (gateway: Gateway): Promise<readonly Ladder[]> =>
  (await gateway.call<{ ladders: Ladder[] }>("encoding___list_ladders")).ladders;
