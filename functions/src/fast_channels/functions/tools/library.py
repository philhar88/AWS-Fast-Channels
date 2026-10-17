"""``library`` target: encoded assets in the media library and the ingest pipeline that produces them.

Assets are ``MediaLibrary`` items written by the IngestPipeline (and by ``origins___register_vod_source`` for
external content). ``ingest`` starts the pipeline; its Step Functions execution name is the correlation id that the
pipeline stamps on the job, the library item and the VOD source (SPEC §9).
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Self
from urllib.parse import urlsplit

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError, model_validator

from fast_channels.core.captions import CaptionOrigin, CaptionProvenance, CaptionsRequest
from fast_channels.core.cost import encode_cost, encode_cost_per_minute
from fast_channels.core.ingest import ASSET_ID_PATTERN, LADDER_VERSION_PATTERN, AssetKind, IngestRequest
from fast_channels.core.naming import mediatailor_name
from fast_channels.functions import aws
from fast_channels.functions.aws import execution_arn
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.library_store import Asset, AssetStatus, LadderStatus, Owner
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, tool

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

logger = Logger(child=True)

EXECUTION_NAME_PATTERN: Final = r"^[A-Za-z0-9_-]{1,80}$"
_ENCODABLE: Final = frozenset({LadderStatus.PROVISIONING, LadderStatus.READY})  # the job template exists
_FINAL: Final = frozenset({AssetStatus.READY, AssetStatus.FAILED})
_MISSING_OBJECT: Final = frozenset({"404", "NoSuchKey", "NotFound"})
_CAUSE_CHARS: Final = 1000  # Step Functions causes can be whole JSON error documents


# --- find_assets / get_asset ---------------------------------------------------------------------------------------


class FindAssetsInput(BaseModel):
    kind: AssetKind | None = Field(
        None, description="content = schedulable programs, slate = break and filler slates, ad = house-ad creatives."
    )
    status: AssetStatus | None = Field(
        None, description="READY assets can be scheduled; INGESTING and ENCODED are still in the pipeline."
    )
    ladder_version: str | None = Field(
        None,
        pattern=LADDER_VERSION_PATTERN,
        description="Only encodes with this ladder version; a channel airs only assets of its own ladder.",
    )
    content_id: str | None = Field(None, max_length=64, description="Only encodes of this catalog title.")
    limit: int = Field(50, ge=1, le=100, description="Maximum number of assets to return.")
    next_token: str | None = Field(None, description="next_token from the previous page; omit for the first page.")


class AssetSummary(BaseModel):
    """One library asset (an encode of one source with one ladder version)."""

    asset_id: str
    kind: AssetKind
    ladder_version: str
    status: AssetStatus
    owner: Owner = Field(description="pipeline = encoded here; agent = external content registered by the agent.")
    title: str | None = None
    content_id: str | None = None
    duration_ms: int | None = Field(None, description="Running time of the content, without ad breaks.")
    break_offset_count: int = Field(description="Number of valid ad-break offsets; library___get_asset lists them.")
    captioned: list[str] = Field(
        [], description="Languages whose caption track has captions; library___get_asset says where each came from."
    )
    vod_source: str | None = Field(None, description="Channel Assembly VOD source; ads have none.")
    updated_at: str

    @classmethod
    def of(cls, asset: Asset) -> Self:
        fields = asset.model_dump(include=set(cls.model_fields) - {"break_offset_count", "captioned"})
        captioned = [c.language for c in asset.captions if c.origin is not CaptionOrigin.NONE]
        return cls(**fields, break_offset_count=len(asset.break_offsets_ms), captioned=captioned)


class FindAssetsOutput(BaseModel):
    assets: list[AssetSummary]
    next_token: str | None = Field(None, description="Pass back to get the next page; absent on the last page.")


@tool("library", access=Access.READ)
def find_assets(request: FindAssetsInput, ctx: ToolContext) -> FindAssetsOutput:
    """List library assets matching every given filter (kind, status, ladder_version, content_id), one compact row
    each, paged with next_token. Use it to find READY content encoded with a channel's ladder version before
    planning, or to see what is still ingesting; library___get_asset has the full asset."""
    page = store.find_assets(
        **request.model_dump(exclude={"limit", "next_token"}), limit=request.limit, next_token=request.next_token
    )
    return FindAssetsOutput(assets=[AssetSummary.of(a) for a in page.items], next_token=page.next_token)


class GetAssetInput(BaseModel):
    asset_id: str = Field(pattern=ASSET_ID_PATTERN, description="Library asset id.")
    ladder_version: str | None = Field(
        None,
        pattern=LADDER_VERSION_PATTERN,
        description="Which encode; omit for the deployment default ladder, else the most recent encode.",
    )


class AssetDetail(AssetSummary):
    break_offsets_ms: list[int] = Field(
        description="Offsets into the content (ms) where every track has a common segment boundary: the only "
        "valid ad-break positions."
    )
    captions: list[CaptionProvenance] = Field(
        [],
        description="Every caption track in ladder order: provided (a file), embedded (in the source), generated "
        "(by the captioner engine; worth a human check) or none (an empty track).",
    )
    source_location: str | None = Field(None, description="Source location of the VOD source; null = the deployment's.")
    packages: dict[str, str] = Field(
        description="Source group (rendition profile) -> manifest path, as registered for external content or "
        "by origins___update_package_configurations."
    )
    source_uri: str | None = None
    correlation_id: str | None = Field(None, description="Ingest execution name or registering agent session.")
    error: str | None = Field(None, description="Why the ingest failed (status FAILED).")
    other_ladder_versions: list[str] = Field(description="Ladder versions this asset is also encoded with.")


@tool("library", access=Access.READ)
def get_asset(request: GetAssetInput, ctx: ToolContext) -> AssetDetail:
    """Get one asset in full: its valid ad-break offsets (ms), VOD source, package paths, the other ladder versions
    it is encoded with, and the ingest error if it FAILED."""
    versions = store.asset_versions(request.asset_id)
    if not versions:
        raise ToolError(
            "NOT_FOUND", f"no asset {request.asset_id} in the library", hint="Search with library___find_assets."
        )
    asset = _choose(versions, request.ladder_version)
    return AssetDetail(
        **AssetSummary.of(asset).model_dump(),
        **asset.model_dump(
            include={
                "break_offsets_ms",
                "captions",
                "source_location",
                "packages",
                "source_uri",
                "correlation_id",
                "error",
            }
        ),
        other_ladder_versions=sorted(v.ladder_version for v in versions if v is not asset),
    )


def _choose(versions: list[Asset], ladder_version: str | None) -> Asset:
    by_version = {v.ladder_version: v for v in versions}
    if ladder_version is not None:
        if ladder_version not in by_version:
            raise ToolError(
                "NOT_FOUND",
                f"{versions[0].asset_id} has no encode with {ladder_version}",
                hint=f"Encoded with: {', '.join(sorted(by_version))}.",
            )
        return by_version[ladder_version]
    default = by_version.get(settings().default_ladder_version)
    return default or max(versions, key=lambda v: v.updated_at)


# --- ingest --------------------------------------------------------------------------------------------------------


class IngestInput(BaseModel):
    source_uri: str = Field(
        pattern=r"^(s3|https)://\S+$",
        max_length=2048,
        description="Mezzanine file: an object in the ingest bucket (s3://<ingest bucket>/<key>, case-sensitive, no "
        "spaces) or an https:// URL.",
    )
    kind: Literal[AssetKind.CONTENT, AssetKind.AD] = Field(
        AssetKind.CONTENT, description="content for a program; ad for a house-ad creative."
    )
    content_id: str | None = Field(None, min_length=1, max_length=64, description="Catalog id of the title.")
    title: str | None = Field(None, max_length=200, description="Display title recorded on the asset.")
    asset_id: str | None = Field(
        None, pattern=ASSET_ID_PATTERN, description="Library id; omit to derive a stable id from the file name."
    )
    cue_points_ms: list[int] = Field(
        [], description="Editorial ad-break positions in ms (catalog cue_points_ms); snapped to the GOP grid."
    )
    duration_ms: int | None = Field(
        None, gt=0, description="Running time in ms (catalog duration_ms); only used for the cost estimate."
    )
    ladder_version: str | None = Field(
        None,
        pattern=LADDER_VERSION_PATTERN,
        description="Ladder version to encode with (PROVISIONING or READY); omit for the deployment default.",
    )
    captions: CaptionsRequest | None = Field(
        None,
        description="Caption files that come with the title (uri relative to the source's folder or s3://, "
        "language, optional format), its spoken_language, and mode: auto (default: provided files, else captions "
        "in the source, else generated), generate (always generate the spoken language) or off (empty tracks).",
    )


class IngestOutput(BaseModel):
    asset_id: str
    ladder_version: str
    kind: AssetKind
    execution_name: str = Field(description="Pass to library___get_ingest_status.")
    execution_arn: str
    started_at: str
    pricing_tier: Literal["BASIC", "PROFESSIONAL"]
    cost_per_minute_usd: float = Field(description="Estimated MediaConvert cost per minute of content.")
    estimated_cost_usd: float | None = Field(None, description="Estimated encode cost; null when duration is unknown.")


@tool("library", access=Access.WRITE, approval=Approval.CONFIRM)
def ingest(request: IngestInput, ctx: ToolContext) -> IngestOutput:
    """Encode a mezzanine file into the media library with a ladder version (MediaConvert, billed per output
    minute). Encode with the ladder of the channel that will air it; pass the catalog duration_ms to get
    estimated_cost_usd. Returns as soon as the ingest pipeline starts; poll library___get_ingest_status with the
    execution_name until done (minutes). Refuses assets already in the library unless they FAILED, and S3 sources
    outside the ingest bucket."""
    config = settings()
    config.require("ingest_state_machine_arn", "ingest_bucket")
    ladder_version = request.ladder_version or _default_ladder_version()
    ladder = _encodable_ladder(ladder_version)
    draft = IngestRequest(
        source_uri=request.source_uri,
        kind=request.kind,
        asset_id=request.asset_id,
        content_id=request.content_id,
        cue_points_ms=request.cue_points_ms,
        captions=request.captions,
        ladder_version=ladder_version,
        metadata={"agent_session": ctx.correlation_id} | ({"title": request.title} if request.title else {}),
    )
    asset_id = draft.resolved_asset_id
    _refuse_duplicate(asset_id, ladder_version)
    _check_source(request.source_uri, config.ingest_bucket)
    # Execution names allow [A-Za-z0-9_-] up to 80 characters; the MediaTailor name rules (<= 64, hashed when
    # longer) are stricter, keep the session id as the prefix for tracing, and give one name per session and asset.
    name = mediatailor_name(ctx.correlation_id, asset_id)
    payload = draft.model_copy(update={"asset_id": asset_id, "correlation_id": name})
    try:
        started = aws.client("stepfunctions").start_execution(
            stateMachineArn=config.ingest_state_machine_arn, name=name, input=payload.model_dump_json(exclude_none=True)
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ExecutionAlreadyExists":
            raise
        raise ToolError(
            "CONFLICT",
            f"this session already ingested {asset_id} (execution {name})",
            hint="Check it with library___get_ingest_status; to encode the file again, pass a different asset_id.",
        ) from error
    logger.info("ingest started", extra={"asset_id": asset_id, "ladder_version": ladder_version, "execution": name})
    return IngestOutput(
        asset_id=asset_id,
        ladder_version=ladder_version,
        kind=request.kind,
        execution_name=name,
        execution_arn=started["executionArn"],
        started_at=store.iso(started["startDate"]),
        pricing_tier=ladder.pricing_tier(),
        cost_per_minute_usd=encode_cost_per_minute(ladder),
        estimated_cost_usd=encode_cost(ladder, request.duration_ms) if request.duration_ms else None,
    )


def _default_ladder_version() -> str:
    if version := settings().default_ladder_version:
        return version
    raise ToolError("VALIDATION", "no default ladder is configured", hint="Pass ladder_version.")


def _encodable_ladder(ladder_version: str) -> Ladder:
    record = store.get_ladder(ladder_version)
    if record is None:
        raise ToolError(
            "NOT_FOUND",
            f"ladder {ladder_version} is not provisioned",
            hint="Call encoding___list_ladders for the provisioned ladder versions.",
        )
    if record.status not in _ENCODABLE:
        raise ToolError(
            "CONFLICT",
            f"ladder {ladder_version} is {record.status}: it has no job template",
            hint="Provision it with encoding___provision_ladder, or use a READY ladder.",
        )
    return record.definition()


def _refuse_duplicate(asset_id: str, ladder_version: str) -> None:
    existing = store.get_asset(asset_id, ladder_version)
    if existing is not None and existing.status is not AssetStatus.FAILED:
        raise ToolError(
            "CONFLICT",
            f"{asset_id} is already {existing.status} with {ladder_version}",
            hint="Use the existing asset (library___get_asset); only FAILED assets are ingested again.",
        )


def _check_source(source_uri: str, ingest_bucket: str) -> None:
    """Refuse S3 sources the pipeline cannot read: outside the ingest bucket, or missing."""
    parts = urlsplit(source_uri)
    if parts.scheme != "s3":
        return
    if parts.netloc != ingest_bucket:
        raise ToolError(
            "VALIDATION",
            f"{source_uri} is outside the ingest bucket",
            hint=f"Copy the file to s3://{ingest_bucket}/ or use an https:// URL; the pipeline reads only that bucket.",
        )
    try:
        aws.client("s3").head_object(Bucket=parts.netloc, Key=parts.path.lstrip("/"))
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code in _MISSING_OBJECT:
            raise ToolError("NOT_FOUND", f"no object {source_uri}", hint="S3 keys are case-sensitive.") from error
        if code == "403":
            raise ToolError("FORBIDDEN", f"cannot read {source_uri}") from error
        raise


# --- get_ingest_status ---------------------------------------------------------------------------------------------


class IngestStatusInput(BaseModel):
    asset_id: str | None = Field(None, pattern=ASSET_ID_PATTERN, description="Asset to check.")
    ladder_version: str | None = Field(
        None, pattern=LADDER_VERSION_PATTERN, description="Ladder version of the encode; omit for the default."
    )
    execution_name: str | None = Field(
        None, pattern=EXECUTION_NAME_PATTERN, description="execution_name returned by library___ingest."
    )

    @model_validator(mode="after")
    def _either(self) -> Self:
        if self.asset_id is None and self.execution_name is None:
            raise ValueError("pass asset_id or execution_name")
        return self


class ExecutionInfo(BaseModel):
    name: str
    status: str = Field(description="RUNNING, SUCCEEDED, FAILED, TIMED_OUT, ABORTED or PENDING_REDRIVE.")
    started_at: str
    stopped_at: str | None = None
    error: str | None = None
    cause: str | None = None


class IngestStatusOutput(BaseModel):
    execution: ExecutionInfo | None = Field(None, description="The ingest pipeline run; null if unknown.")
    asset: AssetSummary | None = Field(None, description="The library item; null until the pipeline creates it.")
    done: bool = Field(description="True once the run has finished, successfully or not.")


@tool("library", access=Access.READ)
def get_ingest_status(request: IngestStatusInput, ctx: ToolContext) -> IngestStatusOutput:
    """Check an ingest: the pipeline run (status, error) and the library asset it produces (status, VOD source,
    duration, number of break offsets); done is true once the run has finished. Pass the execution_name from
    library___ingest, or the asset_id."""
    settings().require("ingest_state_machine_arn")
    ladder_version = request.ladder_version or settings().default_ladder_version
    asset = store.get_asset(request.asset_id, ladder_version) if request.asset_id else None
    name = request.execution_name or (asset.correlation_id if asset else None)
    described = _describe_execution(name) if name else None
    if asset is None and described is not None:
        asset = _asset_of(described)
    if asset is None and described is None:
        raise ToolError(
            "NOT_FOUND",
            f"no ingest found for {request.execution_name or request.asset_id}",
            hint="Check the asset id with library___find_assets.",
        )
    execution = _execution_info(described) if described else None
    done = execution.status != "RUNNING" if execution else asset is not None and asset.status in _FINAL
    return IngestStatusOutput(execution=execution, asset=AssetSummary.of(asset) if asset else None, done=done)


def _describe_execution(name: str) -> dict[str, Any] | None:
    arn = execution_arn(settings().ingest_state_machine_arn, name)
    try:
        described: dict[str, Any] = aws.client("stepfunctions").describe_execution(executionArn=arn)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ExecutionDoesNotExist":
            return None
        raise
    return described


def _asset_of(described: dict[str, Any]) -> Asset | None:
    """The library item of an execution whose input is an IngestRequest (every pipeline trigger sends one)."""
    try:
        request = IngestRequest.model_validate_json(described.get("input") or "{}")
    except ValidationError:
        return None
    return store.get_asset(request.resolved_asset_id, request.ladder_version)


def _execution_info(described: dict[str, Any]) -> ExecutionInfo:
    stopped: datetime | None = described.get("stopDate")
    cause: str | None = described.get("cause")
    return ExecutionInfo(
        name=described["name"],
        status=described["status"],
        started_at=store.iso(described["startDate"]),
        stopped_at=store.iso(stopped) if stopped else None,
        error=described.get("error"),
        cause=cause[:_CAUSE_CHARS] if cause else None,
    )


handler = dispatch.handler("library")
