"""EventBridge event details published on the ``fast-channels`` bus (contracts/events.schema.json).

Every event has source ``fast-channels.<prefix>`` and the detail-type from :data:`DETAIL_TYPES`.
"""

from typing import Any, Final, Literal

from pydantic import BaseModel, Field

from fast_channels.core.catalog import CONTENT_ID_PATTERN
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


class CatalogItemChanged(BaseModel):
    """A title of the reference catalog was added, changed or removed. A CMS sync job puts this event after writing
    the ``ReferenceCatalog`` item; the content enrichment module republishes the title's catalog context object."""

    content_id: str = Field(pattern=CONTENT_ID_PATTERN, max_length=64, description="The title's catalog id.")
    correlation_id: str | None = Field(None, description="Trace id of the change in your CMS.")


class CensusChanged(BaseModel):
    """The number of the deployment's running channels crossed zero: the first channel started, or the last one
    stopped (published by the channel census with the ops module on; at most one census interval late)."""

    running: int = Field(ge=0, description="Running channels now; 0 when the last one stopped.")
    previous: int = Field(ge=0, description="Running channels at the census before.")
    correlation_id: str = Field(description="Name of the census execution that saw the change.")


class ChannelAlert(BaseModel):
    """A Channel Assembly alert of a running channel changed (ops module): Channel Assembly and MediaTailor publish no
    events, so a poller reads ``ListAlerts`` every minute and publishes each alert changed in that minute once."""

    channel: str
    code: str = Field(description="Alert code; for PROGRAM_ALERT the message names the program and its alert.")
    category: str = Field(description="SCHEDULING_ERROR (breaks playback), PLAYBACK_WARNING or INFO.")
    message: str
    resource_arn: str
    last_modified: str = Field(description="UTC ISO-8601 time of the alert's last change.")
    correlation_id: str = Field(description="The poll's scheduled time, which every alert of one poll shares.")


DETAIL_TYPES: Final[dict[type[BaseModel], str]] = {
    IngestRequest: "Ingest Requested",
    AssetReady: "Asset Ready",
    AssetFailed: "Asset Failed",
    LadderReady: "Ladder Ready",
    LadderFailed: "Ladder Failed",
    SchedulePublished: "Schedule Published",
    AutopilotRun: "Autopilot Run",
    CatalogItemChanged: "Catalog Item Changed",
    CensusChanged: "Census Changed",
    ChannelAlert: "Channel Alert",
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
