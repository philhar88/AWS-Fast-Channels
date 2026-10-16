import { ToolCallError, type Gateway } from "../lib/gateway";
import type { ChannelStateChange } from "../lib/tools";
import { pause } from "./agent";
import { ASSETS, CHANNELS, LADDERS, summary } from "./fixtures";

/** Answers the Gateway tools the console calls, with fixture data and in-memory channel state. */
export function mockGateway(): Gateway {
  const states = new Map(CHANNELS.map((channel) => [channel.name, channel.state]));
  const channel = (args: Record<string, unknown>) => {
    const fixture = CHANNELS.find((c) => c.name === args.channel);
    if (!fixture) {
      throw new ToolCallError("NOT_FOUND", `Channel ${String(args.channel)} does not exist`);
    }
    return { ...fixture, state: states.get(fixture.name) ?? fixture.state };
  };
  /** Start and stop are idempotent, like the tools; starting (re)schedules the auto-stop. */
  const transition = (args: Record<string, unknown>, to: ChannelStateChange["state"]): ChannelStateChange => {
    const current = channel(args);
    states.set(current.name, to);
    const autoStop = to === "RUNNING" ? { auto_stop_at: new Date(Date.now() + 4 * 3_600_000).toISOString().replace(/\.\d{3}Z$/, "Z") } : {};
    return { channel: current.name, state: to, ...autoStop, alerts: [] };
  };
  const tools: Record<string, (args: Record<string, unknown>) => unknown> = {
    channels___list_channels: () => ({ channels: CHANNELS.map((c) => summary({ ...c, state: states.get(c.name) ?? c.state })) }),
    channels___get_channel: channel,
    channels___start_channel: (args) => transition(args, "RUNNING"),
    channels___stop_channel: (args) => transition(args, "STOPPED"),
    library___find_assets: () => ({ assets: ASSETS }),
    encoding___list_ladders: () => ({ ladders: LADDERS }),
  };
  return {
    async call<T>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
      await pause(350);
      const handler = tools[tool];
      if (!handler) {
        throw new ToolCallError("VALIDATION", `Unknown tool: ${tool}`);
      }
      return handler(args) as T;
    },
  };
}
