import { useCallback } from "react";
import { useAsync } from "../lib/useAsync";
import { useInterval } from "../lib/useInterval";
import { useViewerBackend } from "./backend";
import type { RailAnswer } from "./railClient";
import { useViewer } from "./viewer";

/** Reasons a rail is about to change: asked again sooner. */
const CHANGING = new Set(["building", "expired"]);

/**
 * The recommender's rail for the signed-in viewer, when the recs module is deployed (`config.railUrl`) and a viewer
 * is signed in; `enabled` is false otherwise, and the app ranks its own rule-based rail. Asked again every minute,
 * and every 10 s while the viewer's model rail is being built. A kids profile asks for the kids lineup
 * (`profile=kids`), never its account's rail.
 */
export function useRail(kids = false): { enabled: boolean; answer: RailAnswer | undefined; error: unknown } {
  const backend = useViewerBackend();
  const { accessToken } = useViewer();
  const enabled = Boolean(backend.config.railUrl && accessToken);
  const load = useCallback(
    async (signal: AbortSignal): Promise<RailAnswer | undefined> =>
      enabled && accessToken ? backend.rail(await accessToken(), kids ? { profile: "kids" } : {}, signal) : undefined,
    [backend, accessToken, enabled, kids],
  );
  const { data, error, reload } = useAsync(load);
  useInterval(reload, data && CHANGING.has(data.reason) ? 10_000 : 60_000);
  return { enabled, answer: data, error };
}
