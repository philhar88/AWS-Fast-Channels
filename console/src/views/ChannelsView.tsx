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
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { useCallback, useState } from "react";
import { ChannelActions, ChannelStateIndicator } from "../components/ChannelActions";
import { EventLog, type LoggedEvent } from "../components/EventLog";
import { OffAirScreen } from "../components/OffAirScreen";
import { ProgramGuide } from "../components/ProgramGuide";
import { useBackend } from "../lib/backend";
import type { ChannelPlayback } from "../lib/config";
import { errorMessage } from "../lib/errors";
import { href } from "../lib/router";
import { formatDateTime } from "../lib/format";
import { getChannel, listChannels, type CdnPlayback, type ChannelSummary } from "../lib/tools";
import { useAsync } from "../lib/useAsync";
import { useInterval } from "../lib/useInterval";
import { playerGate, type PlayerGate } from "../players/gate";
import { HlsPlayer } from "../players/HlsPlayer";
import type { PlayerEvent } from "../players/telemetry";

const MAX_LOGGED_EVENTS = 200;
/** How often an open channel page reads the channel's state: players follow a start or stop made elsewhere. */
const STATE_POLL_MS = 15_000;

export function ChannelsView({ channel }: { channel?: string | undefined }) {
  // One instance per channel: its state, players and event log never carry over to another channel.
  return channel ? <ChannelDetail key={channel} name={channel} /> : <ChannelList />;
}

/** A channel row; `state` is unknown until the Gateway answers. */
type ChannelRow = Omit<ChannelSummary, "state" | "tier"> & { readonly state?: ChannelSummary["state"] };

const LIST_COLUMNS: TableProps.ColumnDefinition<ChannelRow>[] = [
  { id: "name", header: "Name", cell: (c) => <Link href={href({ view: "channels", channel: c.name })}>{c.name}</Link>, isRowHeader: true },
  { id: "state", header: "State", cell: (c) => <ChannelStateIndicator state={c.state} /> },
  { id: "ladder", header: "Ladder", cell: (c) => c.ladder_version },
  { id: "outputs", header: "Outputs", cell: (c) => c.outputs.join(", ") || "-" },
  { id: "owner", header: "Managed by", cell: (c) => (c.owner === "agent" ? "Agent" : "CloudFormation") },
];

/** Channels declared in config.json plus any the agent created, with their live state from the Gateway. */
function ChannelList() {
  const { config, gateway } = useBackend();
  const live = useAsync(useCallback(() => listChannels(gateway), [gateway]));
  const known = new Map(live.data?.map((c) => [c.name, c]));
  const items: ChannelRow[] = [
    ...config.channels.map(
      (c): ChannelRow => known.get(c.name) ?? { name: c.name, owner: "iac", ladder_version: c.ladderVersion, outputs: Object.keys(c.playback) },
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
  useInterval(detail.reload, STATE_POLL_MS);
  const state = detail.data?.state;
  const gate = playerGate(state, detail.error !== undefined);
  const live = detail.data?.playback;
  // An agent channel is known only to the Gateway; a channel without a playback configuration has no live URLs.
  const playback: ChannelPlayback = live && Object.keys(live).length > 0 ? live : (configured?.playback ?? {});
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
  const autoStop = detail.data?.auto_stop_at ? ` · stops automatically ${formatDateTime(detail.data.auto_stop_at)}` : "";
  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          info={state && <ChannelStateIndicator state={state} />}
          description={`Ladder ${ladder ?? "unknown"} · outputs ${Object.keys(playback).join(", ") || "none"}${autoStop}`}
          actions={<ChannelActions name={name} state={state} loading={detail.loading} onChanged={detail.reload} />}
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
        <Playback
          channel={name}
          gate={gate}
          playback={playback}
          otherCdns={detail.data?.cdn_playback?.slice(1) ?? []}
          cdnBaseUrl={config.cdnBaseUrl}
          onEvent={onEvent}
        />
        <EventLog
          events={events}
          onClear={() => {
            setEvents([]);
          }}
        />
        <ProgramGuide channel={name} onAir={state === undefined ? undefined : state === "RUNNING"} />
      </SpaceBetween>
    </ContentLayout>
  );
}

interface PlaybackProps {
  readonly channel: string;
  /** Whether the players may open sessions (players/gate.ts). */
  readonly gate: PlayerGate;
  readonly playback: ChannelPlayback;
  /** The same URLs through the deployment's other CDNs (`delivery.cdns`), listed for copying. */
  readonly otherCdns: readonly CdnPlayback[];
  readonly cdnBaseUrl: string;
  readonly onEvent: (event: PlayerEvent) => void;
}

/**
 * SSAI (stitched) and SGAI (HLS Interstitials) sessions of the channel's HLS output (`hls`), side by side. The
 * players mount when the channel runs, so a Start (here or by the agent) brings them up without a reload, and
 * unmount when it stops.
 */
function Playback({ channel, gate, playback, otherCdns, cdnBaseUrl, onEvent }: PlaybackProps) {
  const outputs = Object.keys(playback).filter((name) => playback[name]?.ssai);
  const output = outputs.includes("hls") ? "hls" : outputs[0];
  const urls = output === undefined ? undefined : playback[output];
  const ssai = urls?.ssai;
  const sgai = urls?.sgai && guidedUrl(urls.sgai);

  return (
    <Container
      header={
        <Header variant="h2" description="One playback configuration (InsertionMode PLAYER_SELECT), two sessions of the same HLS output.">
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
              {gate.kind === "play" ? (
                <HlsPlayer key={ssai} src={ssai} mode="ssai" cdnBaseUrl={cdnBaseUrl} label="SSAI player" onEvent={onEvent} />
              ) : (
                <OffAirScreen channel={channel} gate={gate} />
              )}
            </SpaceBetween>
            <SpaceBetween size="xs">
              <Box variant="h3">Server-guided ad insertion (HLS Interstitials)</Box>
              {!sgai ? (
                <Box color="text-body-secondary">No SGAI URL for this output.</Box>
              ) : gate.kind === "play" ? (
                <HlsPlayer key={sgai} src={sgai} mode="sgai" cdnBaseUrl={cdnBaseUrl} label="SGAI player" onEvent={onEvent} />
              ) : (
                <OffAirScreen channel={channel} gate={gate} />
              )}
            </SpaceBetween>
          </ColumnLayout>
        )}
        {output !== undefined && otherCdns.length > 0 && (
          <KeyValuePairs columns={2} items={otherCdns.flatMap(({ cdn, playback: urls }) => cdnUrls(cdn, output, urls))} />
        )}
      </SpaceBetween>
    </Container>
  );
}

function copyableUrl(label: string, url: string) {
  return {
    label,
    value: <CopyToClipboard variant="inline" textToCopy={url} copyButtonAriaLabel={`Copy ${label} URL`} copySuccessText="URL copied" copyErrorText="URL not copied" />,
  };
}

/** The output's SSAI and SGAI URLs through another CDN. */
function cdnUrls(cdn: string, output: string, playback: ChannelPlayback) {
  const urls = playback[output];
  return [
    ...(urls?.ssai ? [copyableUrl(`SSAI (${output}) via ${cdn}`, urls.ssai)] : []),
    ...(urls?.sgai ? [copyableUrl(`SGAI (${output}) via ${cdn}`, urls.sgai)] : []),
  ];
}

/** Asks MediaTailor for client-side reporting so the asset lists carry TRACKING beacons (players/beacons.ts). */
function guidedUrl(sgai: string): string {
  const url = new URL(sgai, window.location.href);
  url.searchParams.set("aws.insertionMode", "GUIDED");
  url.searchParams.set("aws.reportingMode", "CLIENT");
  return url.href;
}
