"""``Publish``: the last step of every ingest execution, whether it succeeded or failed.

* ``READY``: the asset item becomes READY with its VOD source, ``Asset Ready`` goes on the bus, and the summary
  returned is the execution's output (a ladder provisioner reads its children's outputs).
* ``FAILED``: the item becomes FAILED with the error unless another execution owns it (a previous good encode
  stays listed, e.g. when Prepare refused to encode a READY asset again), ``Asset Failed`` goes on the bus either
  way, and ``{error, cause}`` feeds the execution's Fail state.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from boto3.dynamodb.conditions import Attr
from pydantic import ValidationError

from fast_channels.core.captions import CaptionPlan
from fast_channels.core.events import AssetFailed, AssetReady
from fast_channels.core.ingest import AssetKind, IngestRequest, asset_id_for
from fast_channels.functions import aws
from fast_channels.functions.ingest.library import OWNER, PARENT_EXECUTION_KEY
from fast_channels.functions.ingest.notify import put_event
from fast_channels.functions.media_library import AssetStatus, asset_index, asset_key, now_iso, update_request
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

MAX_CAUSE: Final = 1000
"""Characters of an error cause kept on the item and the event (MediaConvert causes embed the whole job)."""

logger = Logger()
metrics = Metrics()


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """``{"status": "READY", "asset", "encoded", "registered"}`` or ``{"status": "FAILED", "request", "execution",
    "error": {"Error", "Cause"}}``."""
    settings().require("library_table")
    match event["status"]:
        case AssetStatus.READY:
            return ready(event["asset"], event["encoded"], event.get("registered") or [])
        case AssetStatus.FAILED:
            return failed(event["request"], event["execution"], event.get("error") or {})
        case status:
            raise ValueError(f"unknown status {status!r}")


def ready(asset: dict[str, Any], encoded: dict[str, Any], registered: list[dict[str, Any]]) -> dict[str, Any]:
    """Mark the asset READY and announce it; returns the execution output."""
    logger.set_correlation_id(asset["correlation_id"])
    logger.append_keys(asset_id=asset["asset_id"], ladder_version=asset["ladder_version"])
    source = registered[0] if registered else {}
    updated_at = now_iso()
    values: dict[str, Any] = {
        "status": AssetStatus.READY.value,
        **asset_index(asset["kind"], asset["ladder_version"], AssetStatus.READY, updated_at),
        **{
            k: source[k]
            for k in ("vod_source", "vod_source_arn", "source_location", "packages", "alerts")
            if k in source
        },
        "updated_at": updated_at,
    }
    aws.table(settings().library_table).update_item(
        **update_request(
            asset_key(asset["asset_id"], asset["ladder_version"]),
            values,
            ConditionExpression=Attr("correlation_id").eq(asset["correlation_id"]),
        )
    )
    put_event(
        AssetReady(
            asset_id=asset["asset_id"],
            kind=asset["kind"],
            ladder_version=asset["ladder_version"],
            vod_source=source.get("vod_source"),
            duration_ms=encoded["duration_ms"],
            correlation_id=asset["correlation_id"],
            captions=CaptionPlan.model_validate(asset.get("captions") or {}).provenance(),
        )
    )
    metrics.add_dimension(name="kind", value=asset["kind"])
    metrics.add_metric(name="AssetReady", unit=MetricUnit.Count, value=1)
    logger.info("asset ready", extra={"vod_source": source.get("vod_source")})
    return {
        "asset_id": asset["asset_id"],
        "kind": asset["kind"],
        "ladder_version": asset["ladder_version"],
        "label": asset.get("label"),
        "correlation_id": asset["correlation_id"],
        "duration_ms": encoded["duration_ms"],
        "vod_source": source.get("vod_source"),
        "creative": encoded.get("creative"),
    }


def failed(request: dict[str, Any], execution: str, error: dict[str, Any]) -> dict[str, str]:
    """Mark the asset FAILED (if this execution owns it, or nothing does) and announce it in every case."""
    correlation_id = str(request.get("correlation_id") or execution)
    logger.set_correlation_id(correlation_id)
    name, cause = str(error.get("Error") or "States.Unknown"), summarize_cause(error.get("Cause"))
    parsed = _parse(request)
    asset_id = parsed.resolved_asset_id if parsed else _fallback_asset_id(request)
    kind = parsed.kind if parsed else AssetKind.CONTENT
    ladder_version = parsed.ladder_version if parsed else str(request.get("ladder_version", ""))
    logger.append_keys(asset_id=asset_id, ladder_version=ladder_version)
    logger.error("ingest failed", extra={"error": name, "cause": cause})
    if parsed is not None:
        _record_failure(asset_id, ladder_version, kind, correlation_id, f"{name}: {cause}")
    put_event(
        AssetFailed(
            asset_id=asset_id,
            kind=kind,
            ladder_version=ladder_version,
            error=name,
            cause=cause,
            correlation_id=correlation_id,
        )
    )
    metrics.add_dimension(name="kind", value=kind.value)
    metrics.add_metric(name="AssetFailed", unit=MetricUnit.Count, value=1)
    return {"error": name, "cause": cause}


def _record_failure(asset_id: str, ladder_version: str, kind: AssetKind, correlation_id: str, error: str) -> None:
    """Write the FAILED item, unless another execution's item is there (it is left as it is)."""
    updated_at = now_iso()
    values = {
        "asset_id": asset_id,
        "ladder_version": ladder_version,
        "kind": kind.value,
        "owner": OWNER,
        "correlation_id": correlation_id,
        "status": AssetStatus.FAILED.value,
        **asset_index(kind, ladder_version, AssetStatus.FAILED, updated_at),
        "error": error,
        "updated_at": updated_at,
    }
    table = aws.table(settings().library_table)
    try:
        table.update_item(
            **update_request(
                asset_key(asset_id, ladder_version),
                values,
                ConditionExpression=Attr("pk").not_exists() | Attr("correlation_id").eq(correlation_id),
            )
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning("the asset item belongs to another execution; left unchanged")


def summarize_cause(cause: Any) -> str:
    """The human part of a Step Functions error cause, truncated to :data:`MAX_CAUSE` characters: a Lambda
    ``errorMessage``, a MediaConvert job's ``ErrorCode: ErrorMessage``, a failed child execution's
    ``Error: Cause`` (summarized in turn), or the raw text."""
    text = "" if cause is None else str(cause)
    try:
        document = json.loads(text)
    except ValueError:
        return text[:MAX_CAUSE]
    if not isinstance(document, dict):
        return text[:MAX_CAUSE]
    job = document.get("Job", document)
    if "errorMessage" in document:
        text = str(document["errorMessage"])
    elif isinstance(job, dict) and "ErrorMessage" in job:
        text = f"{job.get('ErrorCode', '')}: {job['ErrorMessage']}".lstrip(": ")
    elif "Error" in document:
        text = f"{document['Error']}: {summarize_cause(document.get('Cause'))}".rstrip(": ")
    return text[:MAX_CAUSE]


def _parse(request: dict[str, Any]) -> IngestRequest | None:
    try:
        return IngestRequest.model_validate({k: v for k, v in request.items() if k != PARENT_EXECUTION_KEY})
    except ValidationError:
        return None


def _fallback_asset_id(request: dict[str, Any]) -> str:
    source = request.get("source_uri")
    return str(request.get("asset_id") or (asset_id_for(source) if isinstance(source, str) else "unknown"))
