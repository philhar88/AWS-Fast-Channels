import { useCollection } from "@cloudscape-design/collection-hooks";
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
  warning: "warning",
  error: "error",
};

const COLUMNS: TableProps.ColumnDefinition<LoggedEvent>[] = [
  { id: "time", header: "Time", cell: (e) => formatClockSeconds(e.at), width: 110 },
  { id: "event", header: "Event", cell: (e) => <StatusIndicator type={LEVEL[e.level]}>{e.name}</StatusIndicator>, isRowHeader: true },
  { id: "detail", header: "Detail", cell: (e) => e.detail },
];

/** The player's stitched-ad transitions, playback events and reloads after a lost stream, newest first. */
export function EventLog({ events, onClear }: { events: readonly LoggedEvent[]; onClear: () => void }) {
  const { items, paginationProps } = useCollection([...events], { pagination: { pageSize: 10 } });
  return (
    <Table
      variant="container"
      header={
        <Header
          variant="h2"
          counter={`(${events.length})`}
          description="Stitched ads appear as ad segments behind discontinuities. The player reloads a lost stream with backoff."
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
      empty={<Box color="inherit">Events appear here while the player runs.</Box>}
    />
  );
}
