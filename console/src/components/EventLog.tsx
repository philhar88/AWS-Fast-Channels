import { useCollection } from "@cloudscape-design/collection-hooks";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Header from "@cloudscape-design/components/header";
import Pagination from "@cloudscape-design/components/pagination";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import { formatClockSeconds } from "../lib/format";
import type { PlayerEvent, PlayerEventLevel } from "../players/telemetry";

export interface LoggedEvent extends PlayerEvent {
  readonly id: string;
  readonly at: Date;
}

const LEVEL: Record<PlayerEventLevel, StatusIndicatorProps.Type> = {
  info: "info",
  ad: "in-progress",
  beacon: "success",
  warning: "warning",
  error: "error",
};

const COLUMNS: TableProps.ColumnDefinition<LoggedEvent>[] = [
  { id: "time", header: "Time", cell: (e) => formatClockSeconds(e.at), width: 110 },
  { id: "player", header: "Player", cell: (e) => <Badge color={e.mode === "ssai" ? "red" : "blue"}>{e.mode.toUpperCase()}</Badge>, width: 90 },
  { id: "event", header: "Event", cell: (e) => <StatusIndicator type={LEVEL[e.level]}>{e.name}</StatusIndicator>, isRowHeader: true },
  { id: "detail", header: "Detail", cell: (e) => e.detail },
];

/** Ad, interstitial and beacon events from both players, and their reloads after a lost stream, newest first. */
export function EventLog({ events, onClear }: { events: readonly LoggedEvent[]; onClear: () => void }) {
  const { items, paginationProps } = useCollection([...events], { pagination: { pageSize: 10 } });
  return (
    <Table
      variant="container"
      header={
        <Header
          variant="h2"
          counter={`(${events.length})`}
          description="SSAI ads appear as stitched segments; SGAI ads as HLS Interstitials events with client-side beacons. A player that loses the stream reloads it with backoff."
          actions={
            <Button onClick={onClear} disabled={events.length === 0}>
              Clear
            </Button>
          }
        >
          Ad events
        </Header>
      }
      items={items}
      columnDefinitions={COLUMNS}
      trackBy="id"
      pagination={<Pagination {...paginationProps} />}
      ariaLabels={{ tableLabel: "Ad events" }}
      empty={<Box color="inherit">Events appear here while the players run.</Box>}
    />
  );
}
