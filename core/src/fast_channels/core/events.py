"""EventBridge event details published on the ``fast-channels`` bus (contracts/events.schema.json).

Every event has source ``fast-channels.<prefix>`` and the detail-type from :data:`DETAIL_TYPES`.
"""

from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from fast_channels.core.ingest import AssetKind, IngestRequest


class AssetReady(BaseModel):
    """An asset is encoded, verified and (for content and slates) registered as a VOD source."""

    asset_id: str
    kind: AssetKind
    ladder_version: str
    vod_source: str | None = Field(description="Channel Assembly VOD source name; null for ads.")
    duration_ms: int
    correlation_id: str


class AssetFailed(BaseModel):
    """An ingest execution failed."""

    asset_id: str
    kind: AssetKind
    ladder_version: str
    error: str
    cause: str
    correlation_id: str


class LadderReady(BaseModel):
    """A ladder's job template and kit (filler, break slates, house ads, VAST) are ready."""

    ladder_version: str
    correlation_id: str
    job_template: str | None = Field(None, description="MediaConvert job template name.")


class LadderFailed(BaseModel):
    """Provisioning a ladder failed."""

    ladder_version: str
    correlation_id: str
    error: str
    cause: str


class SchedulePublished(BaseModel):
    """A plan was published to a channel."""

    channel: str
    plan_id: str
    programs: int
    start_ms: int
    end_ms: int
    correlation_id: str


class AutopilotRun(BaseModel):
    """An autopilot run finished."""

    channel: str
    status: Literal["SUCCEEDED", "FAILED"]
    session_id: str
    summary: str


DETAIL_TYPES: Final[dict[type[BaseModel], str]] = {
    IngestRequest: "Ingest Requested",
    AssetReady: "Asset Ready",
    AssetFailed: "Asset Failed",
    LadderReady: "Ladder Ready",
    LadderFailed: "Ladder Failed",
    SchedulePublished: "Schedule Published",
    AutopilotRun: "Autopilot Run",
}


def event_source(prefix: str) -> str:
    """EventBridge ``source`` of a deployment's events."""
    return f"fast-channels.{prefix}"


def put_events_entry(detail: BaseModel, *, prefix: str, event_bus: str) -> dict[str, Any]:
    """PutEvents entry for ``detail`` (raises KeyError for an unknown detail model)."""
    return {
        "Source": event_source(prefix),
        "DetailType": DETAIL_TYPES[type(detail)],
        "Detail": detail.model_dump_json(),
        "EventBusName": event_bus,
    }
