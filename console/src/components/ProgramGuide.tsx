import Alert from "@cloudscape-design/components/alert";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useState } from "react";
import { useBackend } from "../lib/backend";
import { errorMessage } from "../lib/errors";
import { formatDateTime } from "../lib/format";
import { guidePrograms } from "../lib/schedule";
import { useAsync } from "../lib/useAsync";
import { useInterval } from "../lib/useInterval";
import { EpgTimeline } from "./EpgTimeline";
import { ProgramTable } from "./ProgramTable";

const REFRESH_MS = 60_000;

/**
 * The channel's published EPG feed (`epg/<channel>.json`, CONTRACTS §I) as a timeline and a table. `onAir`: the
 * channel is running, so the program scheduled now is on air (when it is stopped, nothing is); undefined while
 * the channel's state is unknown.
 */
export function ProgramGuide({ channel, onAir }: { channel: string; onAir: boolean | undefined }) {
  const backend = useBackend();
  const epg = useAsync(useCallback((signal: AbortSignal) => backend.epg(channel, signal), [backend, channel]));
  const [now, setNow] = useState(Date.now);
  useInterval(() => {
    setNow(Date.now());
    epg.reload();
  }, REFRESH_MS);
  // A start marks the programs it skipped: read the guide again when the channel goes on or off air.
  const [readOnAir, setReadOnAir] = useState(onAir);
  if (readOnAir !== onAir) {
    setReadOnAir(onAir);
    if (readOnAir !== undefined) {
      epg.reload();
    }
  }

  const programs = epg.data?.programs ?? [];
  const listed = guidePrograms(programs, now);
  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={`(${listed.length})`}
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
        {programs.length > 0 && <EpgTimeline programs={programs} now={now} onAir={onAir === true} />}
        <ProgramTable programs={listed} onAirAt={onAir ? now : undefined} />
      </SpaceBetween>
    </Container>
  );
}
