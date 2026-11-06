import { useCallback, useState } from "react";
import { useAsync } from "../lib/useAsync";
import { useInterval } from "../lib/useInterval";
import { useViewerBackend } from "./backend";
import type { ChannelGuide } from "./rail";

/** Every channel's EPG, refreshed each minute, and the time the rails rank at, ticking every 5 s. */
export function useGuides(): { guides: readonly ChannelGuide[] | undefined; error: unknown; now: number } {
  const backend = useViewerBackend();
  const [now, setNow] = useState(() => Date.now());
  const load = useCallback(
    (signal: AbortSignal) =>
      Promise.all(
        backend.config.channels.map(async (channel): Promise<ChannelGuide> => ({ channel, epg: await backend.epg(channel.name, signal) })),
      ),
    [backend],
  );
  const { data, error, reload } = useAsync(load);
  useInterval(reload, 60_000);
  useInterval(() => {
    setNow(Date.now());
  }, 5_000);
  return { guides: data, error, now };
}
