import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import Header from "@cloudscape-design/components/header";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { useCallback, useState } from "react";
import { ChannelActions, ChannelStateIndicator } from "../components/ChannelActions";
import { EventLog, type LoggedEvent } from "../components/EventLog";
import { ProgramGuide } from "../components/ProgramGuide";
import { useBackend } from "../lib/backend";
import type { ChannelPlayback } from "../lib/config";
import { errorMessage } from "../lib/errors";
import { href } from "../lib/router";
import { getChannel, listChannels, type ChannelSummary } from "../lib/tools";
import { useAsync } from "../lib/useAsync";
import { canPlayProfile, PROFILE_LABELS } from "../players/codecs";
import { HlsPlayer } from "../players/HlsPlayer";
import type { PlayerEvent } from "../players/telemetry";

const MAX_LOGGED_EVENTS = 200;

export function ChannelsView({ channel }: { channel?: string | undefined }) {
  return channel ? <ChannelDetail name={channel} /> : <ChannelList />;
}

/** A channel row; `state` is unknown until the Gateway answers. */
type ChannelRow = Omit<ChannelSummary, "state"> & { readonly state?: ChannelSummary["state"] };

const LIST_COLUMNS: TableProps.ColumnDefinition<ChannelRow>[] = [
  { id: "name", header: "Name", cell: (c) => <Link href={href({ view: "channels", channel: c.name })}>{c.name}</Link>, isRowHeader: true },
  { id: "state", header: "State", cell: (c) => <ChannelStateIndicator state={c.state} /> },
  { id: "ladder", header: "Ladder", cell: (c) => c.ladder_version ?? "-" },
  { id: "outputs", header: "Outputs", cell: (c) => c.outputs?.join(", ") ?? "-" },
  { id: "owner", header: "Managed by", cell: (c) => (c.owner === "agent" ? "Agent" : "CloudFormation") },
];

/** Channels declared in config.json plus any the agent created, with their live state from the Gateway. */
function ChannelList() {
  const { config, gateway } = useBackend();
  const live = useAsync(useCallback(() => listChannels(gateway), [gateway]));
  const known = new Map(live.data?.map((c) => [c.name, c]));
  const items: ChannelRow[] = [
    ...config.channels.map(
      (c): ChannelRow => known.get(c.name) ?? { name: c.name, ladder_version: c.ladderVersion, outputs: Object.keys(c.playback) },
    ),
    ...(live.data ?? []).filter((c) => !config.channels.some((configured) => configured.name === c.name)),
  ];
  return (
    <ContentLayout header={<Header variant="h1" description="Linear channels assembled by MediaTailor Channel Assembly and monetised with SSAI or SGAI.">Channels</Header>}>
      <SpaceBetween size="m">
        {live.error !== undefined && (
          <Alert type="warning" header="Live channel state is unavailable">
            {errorMessage(live.error)}
          </Alert>
        )}
        <Table
          variant="container"
          header={
            <Header counter={`(${items.length})`} actions={<Button iconName="refresh" ariaLabel="Refresh" loading={live.loading} onClick={live.reload} />}>
              Channels
            </Header>
          }
          items={items}
          columnDefinitions={LIST_COLUMNS}
          trackBy="name"
          loading={live.loading && live.data === undefined}
          loadingText="Loading channels"
          empty={<Box color="inherit">No channels are configured.</Box>}
        />
      </SpaceBetween>
    </ContentLayout>
  );
}

function ChannelDetail({ name }: { name: string }) {
  const { config, gateway } = useBackend();
  const configured = config.channels.find((c) => c.name === name);
  const detail = useAsync(useCallback(() => getChannel(gateway, name), [gateway, name]));
  const playback: ChannelPlayback = detail.data?.playback ?? configured?.playback ?? {};
  const [events, setEvents] = useState<LoggedEvent[]>([]);

  const onEvent = useCallback((event: PlayerEvent) => {
    setEvents((current) => [{ ...event, id: crypto.randomUUID(), at: new Date() }, ...current].slice(0, MAX_LOGGED_EVENTS));
  }, []);

  if (!configured && detail.error !== undefined) {
    // Not deployed with the stack and not known to the Gateway (for example an agent channel that was deleted).
    return (
      <Alert type="error" header={`Channel ${name} is unavailable`}>
        {errorMessage(detail.error)}
      </Alert>
    );
  }
  const ladder = detail.data?.ladder_version ?? configured?.ladderVersion;
  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          info={detail.data && <ChannelStateIndicator state={detail.data.state} />}
          description={`Ladder ${ladder ?? "unknown"} · outputs ${Object.keys(playback).join(", ") || "none"}`}
          actions={<ChannelActions name={name} state={detail.data?.state} loading={detail.loading} onChanged={detail.reload} />}
        >
          {name}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {detail.error !== undefined && (
          <Alert type="warning" header="Live channel state is unavailable">
            {errorMessage(detail.error)}
          </Alert>
        )}
        <Playback playback={playback} cdnBaseUrl={config.cdnBaseUrl} onEvent={onEvent} />
        <EventLog
          events={events}
          onClear={() => {
            setEvents([]);
          }}
        />
        <ProgramGuide channel={name} />
      </SpaceBetween>
    </ContentLayout>
  );
}

interface PlaybackProps {
  readonly playback: ChannelPlayback;
  readonly cdnBaseUrl: string;
  readonly onEvent: (event: PlayerEvent) => void;
}

/** SSAI (stitched) and SGAI (HLS Interstitials) sessions of the same output, side by side. */
function Playback({ playback, cdnBaseUrl, onEvent }: PlaybackProps) {
  const hlsOutputs = Object.keys(playback).filter((output) => playback[output]?.ssai);
  const dashOutputs = Object.entries(playback).flatMap(([output, urls]) => (urls.dash ? [{ output, url: urls.dash }] : []));
  const [chosen, setChosen] = useState<string>();
  const output = chosen !== undefined && hlsOutputs.includes(chosen) ? chosen : hlsOutputs.includes("hls") ? "hls" : hlsOutputs[0];
  const urls = output === undefined ? undefined : playback[output];
  const ssai = urls?.ssai;
  const sgai = urls?.sgai && guidedUrl(urls.sgai);

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="One playback configuration (InsertionMode PLAYER_SELECT), two sessions. Per-codec outputs pin the player to one codec."
          actions={
            hlsOutputs.length > 1 && output !== undefined ? (
              <SegmentedControl
                label="Output"
                selectedId={output}
                onChange={({ detail }) => {
                  setChosen(detail.selectedId);
                }}
                options={hlsOutputs.map((id) => ({ id, text: PROFILE_LABELS[id] ?? id, disabled: !canPlayProfile(id) }))}
              />
            ) : undefined
          }
        >
          Playback
        </Header>
      }
    >
      <SpaceBetween size="l">
        {ssai === undefined ? (
          <Box color="text-body-secondary">This channel has no HLS output.</Box>
        ) : (
          <ColumnLayout columns={2}>
            <SpaceBetween size="xs">
              <Box variant="h3">Server-side ad insertion (stitched)</Box>
              <HlsPlayer key={ssai} src={ssai} mode="ssai" cdnBaseUrl={cdnBaseUrl} label="SSAI player" onEvent={onEvent} />
            </SpaceBetween>
            <SpaceBetween size="xs">
              <Box variant="h3">Server-guided ad insertion (HLS Interstitials)</Box>
              {sgai ? (
                <HlsPlayer key={sgai} src={sgai} mode="sgai" cdnBaseUrl={cdnBaseUrl} label="SGAI player" onEvent={onEvent} />
              ) : (
                <Box color="text-body-secondary">No SGAI URL for this output.</Box>
              )}
            </SpaceBetween>
          </ColumnLayout>
        )}
        {dashOutputs.length > 0 && (
          <KeyValuePairs
            columns={2}
            items={dashOutputs.map(({ output: id, url }) => ({
              label: `DASH (${id}, SSAI only)`,
              value: (
                <CopyToClipboard
                  variant="inline"
                  textToCopy={url}
                  copyButtonAriaLabel={`Copy ${id} URL`}
                  copySuccessText="URL copied"
                  copyErrorText="URL not copied"
                />
              ),
            }))}
          />
        )}
      </SpaceBetween>
    </Container>
  );
}

/** Asks MediaTailor for client-side reporting so the asset lists carry TRACKING beacons (players/beacons.ts). */
function guidedUrl(sgai: string): string {
  const url = new URL(sgai, window.location.href);
  url.searchParams.set("aws.insertionMode", "GUIDED");
  url.searchParams.set("aws.reportingMode", "CLIENT");
  return url.href;
}
