import { useCollection } from "@cloudscape-design/collection-hooks";
import Box from "@cloudscape-design/components/box";
import Pagination from "@cloudscape-design/components/pagination";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import type { ReactNode } from "react";
import { formatClock } from "../lib/format";
import { breakSummary, type EpgProgram } from "../lib/schedule";

const columns = (now: number | undefined): TableProps.ColumnDefinition<EpgProgram>[] => [
  { id: "start", header: "Start", cell: (p) => formatClock(p.start) },
  { id: "end", header: "End", cell: (p) => formatClock(p.end) },
  { id: "title", header: "Title", cell: (p) => p.title, isRowHeader: true },
  { id: "rating", header: "Rating", cell: (p) => p.rating ?? "-" },
  { id: "breaks", header: "Ad breaks", cell: (p) => breakSummary(p.breaks) },
  { id: "status", header: "Status", cell: (p) => <ProgramStatus program={p} now={now} /> },
];

/** Columns that fit a side panel; the full guide on the Channels page has the others. */
const COMPACT_COLUMNS: ReadonlySet<string> = new Set(["start", "title", "breaks"]);

function ProgramStatus({ program, now }: { program: EpgProgram; now: number | undefined }) {
  if (now !== undefined && Date.parse(program.start) <= now && now < Date.parse(program.end)) {
    return <StatusIndicator type="success">On air</StatusIndicator>;
  }
  return program.status === "AIRED" ? <StatusIndicator type="stopped">Aired</StatusIndicator> : <StatusIndicator type="pending">{program.status === "PLANNED" ? "Planned" : "Published"}</StatusIndicator>;
}

interface ProgramTableProps {
  readonly programs: readonly EpgProgram[];
  /** Marks the program on air at this time. */
  readonly now?: number;
  readonly header?: ReactNode;
  readonly variant?: TableProps.Variant;
  /** Fewer columns, for a narrow container. */
  readonly compact?: boolean;
}

/** EPG programs as a table (Channels program guide, Assistant schedule snapshot). Cells wrap rather than truncate. */
export function ProgramTable({ programs, now, header, variant = "embedded", compact = false }: ProgramTableProps) {
  const { items, paginationProps } = useCollection([...programs], { pagination: { pageSize: 8 } });
  const columnDefinitions = columns(now).filter((column) => !compact || COMPACT_COLUMNS.has(column.id ?? ""));
  return (
    <Table
      variant={variant}
      header={header}
      items={items}
      columnDefinitions={columnDefinitions}
      pagination={programs.length > 8 && <Pagination {...paginationProps} />}
      trackBy="name"
      wrapLines
      stripedRows
      ariaLabels={{ tableLabel: "Programs" }}
      empty={<Box color="inherit">No programs published yet.</Box>}
    />
  );
}
