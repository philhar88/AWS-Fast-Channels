import { createContext, use } from "react";
import { agentCoreClient, type AgentClient } from "./agui";
import type { AppConfig } from "./config";
import { gatewayClient, type Gateway } from "./gateway";
import { fetchEpg, type Epg } from "./schedule";

/** Everything the views talk to. `awsBackend` is the deployed stack; mock mode swaps in fixtures. */
export interface Backend {
  readonly config: AppConfig;
  readonly gateway: Gateway;
  readonly agent: AgentClient;
  readonly epg: (channel: string, signal?: AbortSignal) => Promise<Epg | null>;
}

export const BackendContext = createContext<Backend | null>(null);

export function useBackend(): Backend {
  const backend = use(BackendContext);
  if (backend === null) {
    throw new Error("useBackend() outside a BackendContext");
  }
  return backend;
}

export function awsBackend(config: AppConfig, accessToken: () => Promise<string>): Backend {
  return {
    config,
    gateway: gatewayClient(config.gatewayUrl, accessToken),
    agent: agentCoreClient(config.runtimeInvokeUrl, accessToken),
    epg: (channel, signal) => fetchEpg(config.epgBaseUrl, channel, signal),
  };
}
