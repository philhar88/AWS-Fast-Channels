import { useCollection } from "@cloudscape-design/collection-hooks";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import Pagination from "@cloudscape-design/components/pagination";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import TextFilter from "@cloudscape-design/components/text-filter";
import { useCallback, useEffect } from "react";
import { useBackend } from "../lib/backend";
import { formatDateTime, formatDuration, formatUsd } from "../lib/format";
import { errorNotice, useNotifications } from "../lib/notifications";
import { href } from "../lib/router";
import { findAssets, listLadders, type Asset, type AssetStatus, type Ladder, type LadderStatus } from "../lib/tools";
import { useAsync, type AsyncState } from "../lib/useAsync";

const ASSET_STATUS: Record<AssetStatus, StatusIndicatorProps.Type> = {
  INGESTING: "in-progress",
  ENCODED: "pending",
  READY: "success",
  FAILED: "error",
};

const LADDER_STATUS: Record<LadderStatus, StatusIndicatorProps.Type> = {
  PROPOSED: "info",
  PROVISIONING: "in-progress",
  READY: "success",
  FAILED: "error",
};

const titleCase = (value: string) => value.charAt(0).toUpperCase() + value.slice(1).toLowerCase();

const ASSET_COLUMNS: TableProps.ColumnDefinition<Asset>[] = [
  {
    id: "asset",
    header: "Asset",
    cell: (a) => (
      <>
        <Box>{a.title ?? a.asset_id}</Box>
        {a.title && (
          <Box variant="small" color="text-body-secondary">
            {a.asset_id}
          </Box>
        )}
      </>
    ),
    sortingField: "asset_id",
    isRowHeader: true,
  },
  { id: "kind", header: "Kind", cell: (a) => titleCase(a.kind), sortingField: "kind" },
  { id: "status", header: "Status", cell: (a) => <StatusIndicator type={ASSET_STATUS[a.status]}>{titleCase(a.status)}</StatusIndicator>, sortingField: "status" },
  { id: "duration", header: "Duration", cell: (a) => (a.duration_ms ? formatDuration(a.duration_ms) : "-") },
  { id: "breaks", header: "Break points", cell: (a) => (a.kind === "content" ? a.break_offset_count : "-") },
  { id: "ladder", header: "Ladder", cell: (a) => a.ladder_version },
  { id: "updated", header: "Updated", cell: (a) => formatDateTime(a.updated_at), sortingField: "updated_at" },
];

const LADDER_COLUMNS: TableProps.ColumnDefinition<Ladder>[] = [
  { id: "version", header: "Ladder version", cell: (l) => l.ladder_version, isRowHeader: true },
  {
    id: "status",
    header: "Status",
    cell: (l) => <StatusIndicator type={LADDER_STATUS[l.status]}>{titleCase(l.status)}</StatusIndicator>,
    minWidth: 150,
  },
  { id: "description", header: "Description", cell: (l) => l.description ?? "-" },
  { id: "video", header: "Video", cell: (l) => videoSummary(l) },
  { id: "kit", header: "Kit", cell: (l) => kitSummary(l) },
  { id: "cost", header: "Encode cost / min", cell: (l) => (l.cost_per_minute_usd === undefined ? "-" : formatUsd(l.cost_per_minute_usd)) },
];

/** `16 renditions up to 1080p: AVC, HEVC, AV1`. */
function videoSummary(ladder: Ladder): string {
  const height = ladder.max_height ? ` up to ${ladder.max_height}p` : "";
  const codecs = ladder.codecs?.length ? `: ${ladder.codecs.map((c) => c.toUpperCase()).join(", ")}` : "";
  return ladder.renditions ? `${ladder.renditions} renditions${height}${codecs}` : "-";
}

/** A ladder goes on air only with its kit: filler, break slates and house ads encoded on the same ladder. */
function kitSummary({ kit }: Ladder): string {
  return [kit.filler ? "filler" : "no filler", `${kit.break_slates.length} slates`, `${kit.house_ads.length} house ads`].join(", ");
}

/** Media library (assets) and encoding ladders, read through the Gateway's library and encoding tools. */
export function LibraryView({ tab }: { tab: "assets" | "ladders" }) {
  const { gateway } = useBackend();
  const assets = useAsync(useCallback(() => findAssets(gateway, { limit: 100 }), [gateway]));
  const ladders = useAsync(useCallback(() => listLadders(gateway), [gateway]));
  useErrorNotice("Could not load assets", assets.error);
  useErrorNotice("Could not load ladders", ladders.error);

  return (
    <ContentLayout header={<Header variant="h1" description="Encoded content, slates and house ads, and the ladders they are encoded with.">Library</Header>}>
      <Tabs
        activeTabId={tab}
        onChange={({ detail }) => {
          window.location.hash = href({ view: "library", tab: detail.activeTabId === "ladders" ? "ladders" : "assets" });
        }}
        tabs={[
          { id: "assets", label: "Assets", content: <AssetTable state={assets} /> },
          { id: "ladders", label: "Ladders", content: <LadderTable state={ladders} /> },
        ]}
      />
    </ContentLayout>
  );
}

function useErrorNotice(header: string, error: unknown) {
  const { notify } = useNotifications();
  useEffect(() => {
    if (error !== undefined) {
      notify(errorNotice(header, error));
    }
  }, [error, header, notify]);
}

function AssetTable({ state }: { state: AsyncState<readonly Asset[]> }) {
  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } = useCollection([...(state.data ?? [])], {
    filtering: {
      filteringFunction: (asset, text) =>
        [asset.asset_id, asset.title, asset.kind, asset.status, asset.content_id].some((value) => value?.toLowerCase().includes(text.toLowerCase())),
      empty: <Box color="inherit">The library is empty. Upload a mezzanine to the ingest bucket or ask the agent to ingest one.</Box>,
      noMatch: <Box color="inherit">No assets match the filter.</Box>,
    },
    sorting: { defaultState: { sortingColumn: { sortingField: "updated_at" }, isDescending: true } },
    pagination: { pageSize: 20 },
  });
  return (
    <Table
      {...collectionProps}
      variant="container"
      header={
        <Header counter={`(${state.data?.length ?? 0})`} actions={<Button iconName="refresh" ariaLabel="Refresh assets" loading={state.loading} onClick={state.reload} />}>
          Assets
        </Header>
      }
      items={items}
      columnDefinitions={ASSET_COLUMNS}
      trackBy={(asset) => `${asset.asset_id}/${asset.ladder_version}`}
      loading={state.loading && state.data === undefined}
      loadingText="Loading assets"
      filter={<TextFilter {...filterProps} filteringPlaceholder="Find assets" filteringAriaLabel="Filter assets" countText={`${filteredItemsCount ?? 0} matches`} />}
      pagination={<Pagination {...paginationProps} />}
      stripedRows
    />
  );
}

function LadderTable({ state }: { state: AsyncState<readonly Ladder[]> }) {
  return (
    <Table
      variant="container"
      header={
        <Header
          counter={`(${state.data?.length ?? 0})`}
          description="Ladders are versioned by fingerprint; a channel airs assets of exactly one ladder version."
          actions={<Button iconName="refresh" ariaLabel="Refresh ladders" loading={state.loading} onClick={state.reload} />}
        >
          Ladders
        </Header>
      }
      items={[...(state.data ?? [])]}
      columnDefinitions={LADDER_COLUMNS}
      trackBy="ladder_version"
      loading={state.loading && state.data === undefined}
      loadingText="Loading ladders"
      empty={<Box color="inherit">No ladders found.</Box>}
      wrapLines
    />
  );
}
