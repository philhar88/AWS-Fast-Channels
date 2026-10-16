"""``Prepare``: check an ingest request against its ladder and turn it into a MediaConvert job.

1. Load the ladder version from the library; it must be PROVISIONING (its kit is being encoded) or READY.
2. Probe a source file (MediaConvert Probe, with this function's credentials) and refuse what the ladder cannot
   carry: no video or audio track, HDR into an SDR ladder (every v3 ladder is SDR), or a conversion between the
   50 Hz and 60 Hz frame-rate families unless the ladder sets ``allow_frame_rate_conversion`` (_review.md, media).
3. Snap the cue points to the GOP grid and build the sparse job overrides (ESAM only when a point is off the
   segment grid; ``core.mediaconvert.build_job_overrides``).
4. Write the asset item as INGESTING (with the execution ARN). Another execution that is still running the same
   encode makes this one a duplicate. An encode that is already ENCODED or READY is refused for a source file:
   its VOD source may be on air, and a second encode would overwrite the manifests and segments it serves in place
   (``assets/<asset>/<ladder>/``). Generated kit stills are deterministic and re-encoded by their provisioner.

Refusals raise :class:`IngestRefusedError` and duplicates :class:`IngestInProgressError`; the state machine
catches both by name.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.cost import encode_cost
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import build_job_overrides
from fast_channels.core.naming import digest
from fast_channels.functions import aws
from fast_channels.functions.ingest.library import OWNER, PARENT_EXECUTION_KEY
from fast_channels.functions.media_library import (
    AssetStatus,
    LadderStatus,
    asset_index,
    asset_key,
    from_dynamo,
    ladder_key,
    now_iso,
    to_dynamo,
)
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

HDR_TRANSFER: Final = frozenset({"SMPTE_2084", "ARIB_B67"})  # PQ (HDR10, Dolby Vision) and HLG
_INGESTABLE: Final = frozenset({LadderStatus.PROVISIONING, LadderStatus.READY})
_REFUSED_PROBE: Final = frozenset({"BadRequestException", "NotFoundException", "ForbiddenException"})
_ENCODED: Final = (AssetStatus.ENCODED, AssetStatus.READY)

logger = Logger()


class IngestRefusedError(ValueError):
    """The request cannot be encoded with its ladder; retrying would not help."""


class IngestInProgressError(RuntimeError):
    """Another execution is still ingesting the same asset with the same ladder."""


@dataclass(frozen=True, slots=True)
class Source:
    """What the pipeline needs to know about a mezzanine (from MediaConvert Probe)."""

    duration_ms: int | None
    width: int | None
    height: int | None
    frame_rate: Fraction | None
    hdr: bool
    has_video: bool
    has_audio: bool


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """``{"request": IngestRequest, "execution": name, "execution_arn": arn}`` -> the asset context every later
    state uses."""
    config = settings()
    config.require("library_table", "media_bucket", "prefix")
    request = _request(event["request"])
    correlation_id = request.correlation_id or event["execution"]
    asset_id = request.resolved_asset_id
    logger.set_correlation_id(correlation_id)
    logger.append_keys(asset_id=asset_id, ladder_version=request.ladder_version)

    ladder, job_template = load_ladder(request.ladder_version)
    source = probe(request.source_uri) if request.source_uri else None
    if source is not None and (problems := refusals(source, ladder)):
        raise IngestRefusedError("; ".join(problems))
    notes = warnings(source, ladder) if source else []
    for note in notes:
        logger.warning(note)

    overrides = build_job_overrides(
        ladder=ladder,
        media_bucket=config.media_bucket,
        asset_id=asset_id,
        source_uri=request.source_uri,
        generator=request.generator,
        cue_points_ms=request.cue_points_ms,
        correlation_id=correlation_id,
        app=config.app,
    )
    duration_ms: int | None
    if request.generator is not None:
        duration_ms = ladder.grid.gop_exact(request.generator.duration_gops)
    else:
        duration_ms = source.duration_ms if source else None
    asset = {
        "asset_id": asset_id,
        "kind": request.kind.value,
        "ladder_version": ladder.version,
        "correlation_id": correlation_id,
        "content_id": request.content_id,
        "title": request.metadata.get("title"),
        "label": request.generator.label if request.generator else None,
        "source_uri": request.source_uri or (request.generator.image_s3_uri if request.generator else None),
        "output_prefix": overrides.destination_prefix,
        "cue_points_ms": overrides.snapped_cue_points_ms,
        "warnings": notes,
        "estimated_cost_usd": encode_cost(ladder, duration_ms) if duration_ms else None,
    }
    _claim(
        asset,
        metadata=request.metadata,
        job_template=job_template,
        execution_arn=event["execution_arn"],
        replaces_encodes=request.generator is not None,
    )
    logger.info("prepared", extra={"esam": "Esam" in overrides.settings, "cue_points_ms": asset["cue_points_ms"]})
    return {
        **asset,
        "ladder": ladder.model_dump(mode="json"),
        "job": {
            "JobTemplate": job_template,
            "Settings": overrides.settings,
            "UserMetadata": overrides.user_metadata,
            # The job-level mode replaces the template's: generator (slate, house-ad) inputs never accelerate.
            "AccelerationSettings": {"Mode": overrides.acceleration or ladder.options.acceleration},
            # CreateJob idempotency (1 min, <= 64 chars) for retries of the Transcode state.
            "ClientRequestToken": digest(event["execution"], 32),
        },
    }


def _request(raw: dict[str, Any]) -> IngestRequest:
    fields = {k: v for k, v in raw.items() if k != PARENT_EXECUTION_KEY}
    try:
        return IngestRequest.model_validate(fields)
    except ValidationError as error:
        raise IngestRefusedError(f"invalid ingest request: {error}") from None


def load_ladder(ladder_version: str) -> tuple[Ladder, str]:
    """The ladder definition and job template of a provisioned ladder version."""
    item = aws.table(settings().library_table).get_item(Key=ladder_key(ladder_version)).get("Item")
    if item is None or item.get("status") not in _INGESTABLE or not item.get("job_template"):
        raise IngestRefusedError(f"ladder {ladder_version} is not provisioned (no PROVISIONING or READY ladder item)")
    stored = from_dynamo(item.get("ladder"))
    ladder = Ladder.model_validate(stored) if stored else Ladder.builtin(ladder_version.partition("@")[0])
    if ladder.version != ladder_version:
        raise IngestRefusedError(f"the stored definition of {ladder_version} has version {ladder.version}")
    return ladder, str(item["job_template"])


def probe(uri: str) -> Source:
    """Probe ``uri`` (s3:// or https://) with MediaConvert."""
    try:
        [result] = aws.client("mediaconvert").probe(InputFiles=[{"FileUrl": uri}])["ProbeResults"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _REFUSED_PROBE:
            raise IngestRefusedError(f"cannot probe {uri}: {error.response['Error'].get('Message', '')}") from None
        raise
    container = result.get("Container", {})
    tracks = container.get("Tracks", [])
    video = next((t.get("VideoProperties", {}) for t in tracks if t.get("TrackType") == "video"), None)
    metadata = (video or {}).get("CodecMetadata", {})
    rate = (video or {}).get("FrameRate") or metadata.get("CodedFrameRate") or {}
    transfer = (video or {}).get("TransferCharacteristics") or metadata.get("TransferCharacteristics")
    dolby_vision = metadata.get("DolbyVision", {})
    duration = container.get("Duration")
    return Source(
        duration_ms=round(duration * 1000) if duration else None,
        width=(video or {}).get("Width"),
        height=(video or {}).get("Height"),
        frame_rate=Fraction(rate["Numerator"], rate["Denominator"]) if rate.get("Denominator") else None,
        hdr=transfer in HDR_TRANSFER
        or metadata.get("Hdr10PlusPresence") == "PRESENT"
        or "PRESENT" in (dolby_vision.get("BaseLayer"), dolby_vision.get("Rpu")),
        has_video=video is not None,
        has_audio=any(t.get("TrackType") == "audio" for t in tracks),
    )


def frame_rate_family(fps: float) -> int:
    """Display-rate family: 50 for 12.5/25/50 fps, 60 for 24/30/60 fps and their NTSC (x 1000/1001) rates."""
    return 50 if round(fps * 2) % 25 == 0 else 60


def refusals(source: Source, ladder: Ladder) -> list[str]:
    """Why ``source`` cannot be encoded with ``ladder`` (empty when it can)."""
    problems = []
    if not source.has_video:
        problems.append("the source has no video track")
    if not source.has_audio:
        problems.append("the source has no audio track; every rendition of the ladder carries audio")
    if source.hdr:
        problems.append("the source is HDR (PQ or HLG) and the ladder is SDR")
    if (
        source.frame_rate is not None
        and not ladder.options.allow_frame_rate_conversion
        and frame_rate_family(float(source.frame_rate)) != frame_rate_family(ladder.frame_rate.fps)
    ):
        problems.append(
            f"converting {float(source.frame_rate):.3f} fps to the ladder's {ladder.frame_rate.fps:.3f} fps crosses "
            "the 50/60 Hz families (judder); use a ladder of the source's family or set allow_frame_rate_conversion"
        )
    return problems


def warnings(source: Source, ladder: Ladder) -> list[str]:
    """Encodable but noteworthy properties of ``source`` (stored on the asset item)."""
    notes = []
    if source.height is not None and source.height < ladder.max_height:
        notes.append(f"the source is {source.height} lines high; rungs above it are upscaled")
    if source.frame_rate is not None and source.frame_rate != ladder.frame_rate.fraction:
        notes.append(f"the frame rate is converted from {float(source.frame_rate):.3f} to {ladder.frame_rate.fps:.3f}")
    return notes


def _claim(
    asset: dict[str, Any], *, metadata: dict[str, str], job_template: str, execution_arn: str, replaces_encodes: bool
) -> None:
    """Put the INGESTING asset item: over nothing, a FAILED encode, this execution's own item or a stale claim.

    An execution that ended without reaching PublishFailure (a kit sibling aborted by its provisioner, a timeout)
    leaves its item INGESTING (or ENCODED); the execution ARN recorded on the item shows that it no longer runs,
    and its claim is taken over. ``replaces_encodes`` allows replacing an ENCODED or READY item (kit stills).
    """
    updated_at = now_iso()
    item = {
        **{k: v for k, v in asset.items() if v is not None},
        **asset_key(asset["asset_id"], asset["ladder_version"]),
        **asset_index(asset["kind"], asset["ladder_version"], AssetStatus.INGESTING, updated_at),
        "status": AssetStatus.INGESTING.value,
        "owner": OWNER,
        "metadata": metadata,
        "job_template": job_template,
        "execution_arn": execution_arn,
        "updated_at": updated_at,
    }
    table = aws.table(settings().library_table)
    claimable = [AssetStatus.FAILED, *(_ENCODED if replaces_encodes else ())]
    unclaimed = (
        Attr("pk").not_exists()
        | Attr("status").is_in([status.value for status in claimable])
        | Attr("correlation_id").eq(asset["correlation_id"])
    )
    if _put_if(table, item, unclaimed):
        return
    holder = from_dynamo(table.get_item(Key=asset_key(asset["asset_id"], asset["ladder_version"])).get("Item") or {})
    if holder.get("status") in {AssetStatus.INGESTING, AssetStatus.ENCODED} and _running(holder.get("execution_arn")):
        raise IngestInProgressError(
            f"{asset['asset_id']} is already being ingested with {asset['ladder_version']} by another execution"
        )
    if holder.get("status") in _ENCODED and not replaces_encodes:
        raise IngestRefusedError(
            f"{asset['asset_id']} is already {holder['status']} with {asset['ladder_version']}, and encoding it again "
            "would overwrite the media its VOD source serves; upload the file under a new key, or pass a new "
            "asset_id, to encode it again"
        )
    same_holder = Attr("correlation_id").eq(holder["correlation_id"]) if holder else unclaimed
    if not _put_if(table, item, same_holder):
        raise IngestInProgressError(
            f"{asset['asset_id']} is already being ingested with {asset['ladder_version']} by another execution"
        )
    logger.warning("took over the claim of an execution that is no longer running", extra={"previous": holder})


def _put_if(table: Any, item: dict[str, Any], condition: Any) -> bool:
    try:
        table.put_item(Item=to_dynamo(item), ConditionExpression=condition)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _running(execution_arn: str | None) -> bool:
    if not execution_arn:
        return False
    return str(aws.client("stepfunctions").describe_execution(executionArn=execution_arn)["status"]) == "RUNNING"
