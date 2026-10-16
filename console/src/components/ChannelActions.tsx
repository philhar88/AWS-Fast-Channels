import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { useState } from "react";
import { useBackend } from "../lib/backend";
import { formatDateTime } from "../lib/format";
import { errorNotice, useNotifications, type Notice } from "../lib/notifications";
import { startChannel, stopChannel, type ChannelState, type ChannelStateChange } from "../lib/tools";

export function ChannelStateIndicator({ state }: { state: ChannelState | undefined }) {
  switch (state) {
    case "RUNNING":
      return <StatusIndicator type="success">Running</StatusIndicator>;
    case "STOPPED":
      return <StatusIndicator type="stopped">Stopped</StatusIndicator>;
    case "MISSING":
      // In the media library but not in Channel Assembly (deleted outside the deployment).
      return <StatusIndicator type="warning">Missing</StatusIndicator>;
    case undefined:
      return <StatusIndicator type="pending">Unknown</StatusIndicator>;
  }
}

type Action = "start" | "stop";

/** Success, or a warning when the channel reports SCHEDULING_ERROR alerts (they break playback). */
function stateChangeNotice(name: string, result: ChannelStateChange): Notice {
  const [first, ...more] = result.alerts.filter((alert) => alert.category === "SCHEDULING_ERROR");
  const lines = [
    result.auto_stop_at && `It stops automatically at ${formatDateTime(result.auto_stop_at)}.`,
    first && `Scheduling error ${first.code}: ${first.message}${more.length > 0 ? ` (and ${more.length} more)` : ""}`,
  ];
  return {
    type: first ? "warning" : "success",
    header: `${name} is ${result.state === "RUNNING" ? "running" : "stopped"}`,
    content: lines.filter(Boolean).join(" ") || undefined,
  };
}

const COPY: Record<Action, { title: string; body: string; button: string }> = {
  start: {
    title: "Start channel",
    body: "Viewers can watch the channel as soon as it starts, and Channel Assembly bills for every hour it runs. It stops automatically after the deployment's maximum runtime.",
    button: "Start channel",
  },
  stop: {
    title: "Stop channel",
    body: "Playback stops for every viewer until the channel is started again. Published programs stay in the schedule.",
    button: "Stop channel",
  },
};

interface ChannelActionsProps {
  readonly name: string;
  readonly state: ChannelState | undefined;
  readonly loading: boolean;
  readonly onChanged: () => void;
}

/**
 * Start/Stop through the Gateway with the operator's own token. Both tools are CONFIRM tools: when the agent calls
 * them it must ask first; here the confirmation dialog is that approval, and Cedar still decides by group.
 */
export function ChannelActions({ name, state, loading, onChanged }: ChannelActionsProps) {
  const { gateway } = useBackend();
  const { notify } = useNotifications();
  const [confirming, setConfirming] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (action: Action) => {
    setBusy(true);
    try {
      const result = await (action === "start" ? startChannel : stopChannel)(gateway, name);
      notify(stateChangeNotice(name, result));
      onChanged();
    } catch (error) {
      notify(errorNotice(`Could not ${action} ${name}`, error));
    } finally {
      setBusy(false);
      setConfirming(null);
    }
  };

  return (
    <>
      <SpaceBetween direction="horizontal" size="xs">
        <Button iconName="refresh" ariaLabel="Refresh channel state" loading={loading} onClick={onChanged} />
        <Button
          disabled={state !== "RUNNING"}
          onClick={() => {
            setConfirming("stop");
          }}
        >
          Stop
        </Button>
        <Button
          variant="primary"
          disabled={state !== "STOPPED"}
          onClick={() => {
            setConfirming("start");
          }}
        >
          Start
        </Button>
      </SpaceBetween>
      {confirming && (
        <Modal
          visible
          header={`${COPY[confirming].title} ${name}?`}
          onDismiss={() => {
            setConfirming(null);
          }}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  disabled={busy}
                  onClick={() => {
                    setConfirming(null);
                  }}
                >
                  Cancel
                </Button>
                <Button variant="primary" loading={busy} onClick={() => void run(confirming)}>
                  {COPY[confirming].button}
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          {COPY[confirming].body}
        </Modal>
      )}
    </>
  );
}
