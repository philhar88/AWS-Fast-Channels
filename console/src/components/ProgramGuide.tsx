import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useEffect, useState } from "react";
import { useBackend } from "../lib/backend";
import { errorMessage } from "../lib/errors";
import { formatDateTime } from "../lib/format";
import { useAsync } from "../lib/useAsync";
import { EpgTimeline } from "./EpgTimeline";
import { ProgramTable } from "./ProgramTable";

const REFRESH_MS = 60_000;

/** The channel's published EPG feed (`epg/<channel>.json`, CONTRACTS §I) as a timeline and a table. */
export function ProgramGuide({ channel }: { channel: string }) {
  const backend = useBackend();
  const epg = useAsync(useCallback((signal: AbortSignal) => backend.epg(channel, signal), [backend, channel]));
  const [now, setNow] = useState(Date.now);
  const { reload } = epg;

  useEffect(() => {
    const timer = window.setInterval(() => {
      setNow(Date.now());
      reload();
    }, REFRESH_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [reload]);

  const programs = epg.data?.programs ?? [];
  const upcoming = programs.filter((p) => Date.parse(p.end) > now);
  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={`(${upcoming.length})`}
          description={epg.data ? `EPG generated ${formatDateTime(epg.data.generated_at)}` : "No EPG published for this channel yet."}
          actions={<Button iconName="refresh" ariaLabel="Refresh program guide" loading={epg.loading} onClick={epg.reload} />}
        >
          Program guide
        </Header>
      }
    >
      <SpaceBetween size="m">
        {epg.error !== undefined && (
          <Alert type="error" header="The EPG could not be loaded">
            {errorMessage(epg.error)}
          </Alert>
        )}
        {programs.length > 0 && <EpgTimeline programs={programs} now={now} />}
        <ProgramTable programs={upcoming} now={now} />
      </SpaceBetween>
    </Container>
  );
}
