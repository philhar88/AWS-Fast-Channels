import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback } from "react";
import { errorMessage } from "../../lib/errors";
import { formatClockSeconds } from "../../lib/format";
import { useAsync } from "../../lib/useAsync";
import { useInterval } from "../../lib/useInterval";
import { droppedLine, modeLine, podLine, policyLine, type BreakDecision } from "../adPanel";
import { useViewerBackend } from "../backend";

const POLL_MS = 20_000;

/**
 * Why these ads: the Pod Decision Service's decision for each break of this session, from its decision records.
 * Demo only. The records take about a minute to be queryable, and the panel refreshes every 20 s.
 */
export function AdPanel({ sid, accessToken }: { sid: string | undefined; accessToken: () => Promise<string> }) {
  const backend = useViewerBackend();
  const load = useCallback(
    async (signal: AbortSignal) => (sid ? backend.decisions(sid, await accessToken(), signal) : null),
    [accessToken, backend, sid],
  );
  const { data, error, loading, reload } = useAsync(load);
  useInterval(reload, POLL_MS);
  const breaks = data?.breaks ?? [];
  const [latest] = breaks;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="What the Pod Decision Service decided for each break of this session, from its decision records. Demo only: a production viewer app never shows a tier."
          actions={
            <Button iconName="refresh" ariaLabel="Refresh the decisions" loading={loading && data !== undefined} onClick={reload} disabled={!sid} />
          }
        >
          Why these ads
        </Header>
      }
    >
      <SpaceBetween size="m">
        {!sid && <Box color="text-status-inactive">Waiting for the session to start…</Box>}
        {error !== undefined && <Alert type="warning">{errorMessage(error)}</Alert>}
        {sid && data && breaks.length === 0 && (
          <Box color="text-status-inactive">No break decided for this session yet. Decisions appear about a minute after a break starts.</Box>
        )}
        {latest && (
          <KeyValuePairs
            columns={2}
            items={[
              { label: "Privacy mode", value: modeLine(latest) },
              { label: "Value tier", value: <TierBadge tier={latest.tier} /> },
              { label: "Ad-load policy", value: policyLine(latest) },
              { label: "Last break", value: podLine(latest) },
            ]}
          />
        )}
        {breaks.map((decision) => (
          <BreakRow key={decision.decision_id} decision={decision} />
        ))}
      </SpaceBetween>
    </Container>
  );
}

function TierBadge({ tier }: { tier: string | null }) {
  if (tier === null) {
    return <Box color="text-status-inactive">none (control plan)</Box>;
  }
  return <Badge color={tier === "light" ? "green" : tier === "holdout" ? "severity-neutral" : "blue"}>{tier}</Badge>;
}

function BreakRow({ decision }: { decision: BreakDecision }) {
  const dropped = droppedLine(decision);
  return (
    <Box>
      <Box variant="awsui-key-label">
        Break {decision.evid}, decided at {formatClockSeconds(decision.at)}
      </Box>
      <Box>{podLine(decision)}</Box>
      <Box variant="small">{policyLine(decision)}</Box>
      {dropped && <Box variant="small">{dropped}</Box>}
    </Box>
  );
}
