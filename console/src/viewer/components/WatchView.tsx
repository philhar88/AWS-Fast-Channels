import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useCallback, useState } from "react";
import { EventLog, type LoggedEvent } from "../../components/EventLog";
import { formatClock, formatDuration } from "../../lib/format";
import type { PlayedBreak } from "../../players/breaks";
import type { PlayerEvent } from "../../players/telemetry";
import { useViewerBackend } from "../backend";
import type { Persona } from "../config";
import type { Source } from "../playback";
import { channelRail } from "../rail";
import { viewerHref } from "../router";
import { useViewer } from "../viewer";
import { useGuides } from "../useGuides";
import { AdPanel } from "./AdPanel";
import { ViewerPlayer } from "./ViewerPlayer";

const MAX_EVENTS = 200;

/** One channel: the player, what this session is and what its breaks played, and why these ads. */
export function WatchView({ channel: name, persona }: { channel: string; persona: Persona }) {
  const backend = useViewerBackend();
  const viewer = useViewer();
  const channel = backend.config.channels.find((c) => c.name === name);
  if (!channel) {
    return (
      <Alert type="error" header={`There is no channel ${name}`} action={<Button href={viewerHref({ view: "home" })}>Back to the rails</Button>}>
        This deployment does not list it.
      </Alert>
    );
  }
  // A new player, and a new session, per channel and per viewer.
  return <Watching key={`${persona.id}/${name}`} channelName={name} persona={persona} accessToken={viewer.accessToken} />;
}

function Watching({
  channelName,
  persona,
  accessToken,
}: {
  channelName: string;
  persona: Persona;
  accessToken: (() => Promise<string>) | undefined;
}) {
  const backend = useViewerBackend();
  const channel = backend.config.channels.find((c) => c.name === channelName);
  const { guides, now } = useGuides();
  const [source, setSource] = useState<Source>();
  const [sid, setSid] = useState<string>();
  const [played, setPlayed] = useState<readonly PlayedBreak[]>([]);
  const [events, setEvents] = useState<readonly LoggedEvent[]>([]);

  const openSource = useCallback((signal: AbortSignal) => {
    if (!channel) {
      return Promise.reject(new Error(`no channel ${channelName}`));
    }
    return backend.open(channel, accessToken, signal);
  }, [accessToken, backend, channel, channelName]);
  const onSource = useCallback((value: Source) => {
    setSource(value);
    setSid(value.sid);
  }, []);
  const onBreak = useCallback((value: PlayedBreak) => {
    setPlayed((current) => [value, ...current].slice(0, 5));
  }, []);
  const onEvent = useCallback((event: PlayerEvent) => {
    setEvents((current) => [{ ...event, id: crypto.randomUUID(), at: new Date() }, ...current].slice(0, MAX_EVENTS));
  }, []);

  const tile = guides && channelRail(persona.genres, guides, now).find((t) => t.channel.name === channelName);
  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={tile?.now ? `Now: ${tile.now.title}${tile.next ? ` · next at ${formatClock(tile.next.start)}: ${tile.next.title}` : ""}` : undefined}
        actions={<Button href={viewerHref({ view: "home" })}>Back to the rails</Button>}
      >
        {channelName}
      </Header>
      <Grid gridDefinition={[{ colspan: { default: 12, m: 8 } }, { colspan: { default: 12, m: 4 } }]}>
        <ViewerPlayer
          openSource={openSource}
          cdnBaseUrl={backend.config.cdnBaseUrl}
          label={`${channelName} player`}
          onSource={onSource}
          onSessionId={setSid}
          onBreak={onBreak}
          onEvent={onEvent}
        />
        <SpaceBetween size="l">
          <Container header={<Header variant="h2">This session</Header>}>
            <KeyValuePairs
              items={[
                { label: "Watching as", value: `${persona.name}${accessToken ? "" : " (guest)"}` },
                {
                  label: "Playback",
                  value: source ? (source.cid ? `Ticket ${source.cid}` : `Anonymous: ${source.anonymous ?? "no ticket"}`) : "Opening…",
                  info: source && !source.cid ? <Box variant="small">Contextual ads on the control plan</Box> : undefined,
                },
                { label: "MediaTailor session", value: sid ?? "Not known yet" },
                {
                  label: "Breaks played",
                  value:
                    played.length === 0 ? (
                      "None yet"
                    ) : (
                      <SpaceBetween size="xxs">
                        {played.map((b, index) => (
                          <Box key={`${index}-${b.ads}-${b.slateSeconds}`}>
                            {b.ads} ad{b.ads === 1 ? "" : "s"}
                            {b.slateSeconds >= 1 ? ` + ${formatDuration(b.slateSeconds * 1000)} of slate` : ""}
                          </Box>
                        ))}
                      </SpaceBetween>
                    ),
                },
              ]}
            />
          </Container>
          {backend.config.adPanelUrl !== null && accessToken && <AdPanel sid={sid} accessToken={accessToken} />}
          {backend.config.adPanelUrl !== null && !accessToken && (
            <Container header={<Header variant="h2">Why these ads</Header>}>
              <Box>A guest's session is CONTEXTUAL: contextual ads on the control plan, the same for every guest. Sign in to see a viewer's decisions.</Box>
            </Container>
          )}
        </SpaceBetween>
      </Grid>
      <EventLog
        events={events}
        onClear={() => {
          setEvents([]);
        }}
      />
    </SpaceBetween>
  );
}
