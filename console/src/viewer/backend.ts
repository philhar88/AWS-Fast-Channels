/**
 * Everything the viewer app calls, behind one interface so mock mode can stand in: Cognito sign-in, the playback
 * API and plans, the public EPG feeds and the demo's ad panel API.
 */
import { createContext, use } from "react";
import { fetchEpg, type Epg } from "../lib/schedule";
import { fetchDecisions, type SessionDecisions } from "./adPanel";
import { refreshTokens, signIn, type Tokens } from "./auth";
import type { ViewerChannel, ViewerConfig } from "./config";
import { openChannel, type Source } from "./playback";

export interface ViewerBackend {
  readonly config: ViewerConfig;
  signIn(username: string, password: string): Promise<Tokens>;
  refresh(refreshToken: string): Promise<Tokens>;
  /** The URL of one new session of `channel` (a ticket when `accessToken` is given). */
  open(channel: ViewerChannel, accessToken: (() => Promise<string>) | undefined, signal: AbortSignal): Promise<Source>;
  epg(channel: string, signal?: AbortSignal): Promise<Epg | null>;
  /** The PDS decisions of the viewer's own session `sid`; only when `config.adPanelUrl` is set. */
  decisions(sid: string, accessToken: string, signal?: AbortSignal): Promise<SessionDecisions>;
}

export function awsBackend(config: ViewerConfig): ViewerBackend {
  return {
    config,
    signIn: (username, password) => signIn(config.region, config.userPoolClientId, username, password),
    refresh: (refreshToken) => refreshTokens(config.region, config.userPoolClientId, refreshToken),
    open: (channel, accessToken, signal) =>
      openChannel(channel, { playbackUrl: config.playbackUrl, planBaseUrl: config.planBaseUrl, accessToken }, signal),
    epg: (channel, signal) => fetchEpg(config.epgBaseUrl, channel, signal),
    decisions: (sid, accessToken, signal) => {
      if (!config.adPanelUrl) {
        return Promise.reject(new Error("The ad panel is not deployed (modules.viewer.ad_panel)"));
      }
      return fetchDecisions(config.adPanelUrl, accessToken, sid, signal);
    },
  };
}

export const ViewerBackendContext = createContext<ViewerBackend | null>(null);

export function useViewerBackend(): ViewerBackend {
  const backend = use(ViewerBackendContext);
  if (backend === null) {
    throw new Error("useViewerBackend() outside a ViewerBackendContext");
  }
  return backend;
}
