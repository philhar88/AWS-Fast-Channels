"""Starts an ingest execution for an EventBridge event, named after the request's correlation id.

Two rules invoke this function asynchronously (retries, then the pipeline's dead-letter queue):

* ``Object Created`` in the ingest bucket, for video file extensions. The request is the object plus its optional
  JSON sidecar ``<key>.json`` (any IngestRequest field except ``source_uri`` and ``generator``; upload it first)
  and, when the sidecar has no cue points, the v2-compatible ``AdOffsets`` object tag (milliseconds separated by
  spaces or commas, e.g. ``30000 90000``).
* ``Ingest Requested`` on the ``fast-channels`` bus; the detail is an IngestRequest.

The sidecar is read here, before StartExecution, so that its ``correlation_id`` becomes the execution name
(SPEC §9). Without one, the name is derived from the S3 sequencer (or the event id): a redelivered event then
resolves to the same execution instead of starting a second encode.
"""

import json
import re
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from pydantic import ValidationError

from fast_channels.core.events import DETAIL_TYPES, AssetFailed
from fast_channels.core.ingest import AssetKind, IngestRequest, asset_id_for
from fast_channels.core.naming import digest
from fast_channels.functions import aws
from fast_channels.functions.ingest.notify import put_event
from fast_channels.functions.ingest.storage import S3Store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

AD_OFFSETS_TAG: Final = "adoffsets"
"""v2 object tag with ad-break offsets in ms; matched case-insensitively (v2 accepted ``AdOffsets``,
``ad_offsets``, ``adoffsets``)."""
SIDECAR_SUFFIX: Final = ".json"
INGEST_REQUESTED: Final = DETAIL_TYPES[IngestRequest]

logger = Logger()


class InvalidEventError(ValueError):
    """The event cannot become an ingest request."""


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, str]:
    """Start the ingest execution of ``event``; invalid requests are reported as ``Asset Failed`` events."""
    config = settings()
    config.require("ingest_state_machine_arn", "default_ladder_version")
    try:
        request = request_for(event, default_ladder_version=config.default_ladder_version)
    except (InvalidEventError, ValidationError) as error:
        return _reject(event, error, default_ladder_version=config.default_ladder_version)
    name = str(request.correlation_id)
    logger.set_correlation_id(name)
    logger.append_keys(asset_id=request.resolved_asset_id, ladder_version=request.ladder_version)
    sfn = aws.client("stepfunctions")
    try:
        execution = sfn.start_execution(
            stateMachineArn=config.ingest_state_machine_arn,
            name=name,
            input=request.model_dump_json(exclude_none=True),
        )
    except sfn.exceptions.ExecutionAlreadyExists:
        logger.warning("an ingest execution with this correlation id already exists; event ignored")
        return {"status": "DUPLICATE", "correlation_id": name}
    logger.info("ingest started", extra={"execution_arn": execution["executionArn"]})
    return {"status": "STARTED", "correlation_id": name, "execution_arn": execution["executionArn"]}


def request_for(event: dict[str, Any], *, default_ladder_version: str) -> IngestRequest:
    """The IngestRequest of an EventBridge event, with its correlation id resolved."""
    match event.get("source"), event.get("detail-type"):
        case "aws.s3", "Object Created":
            request, seed = _object_request(event["detail"], default_ladder_version), _sequencer(event)
        case _, detail_type if detail_type == INGEST_REQUESTED:
            request, seed = IngestRequest.model_validate(event["detail"]), str(event.get("id", ""))
        case source, detail_type:
            raise InvalidEventError(f"unsupported event {source!r} / {detail_type!r}")
    if request.correlation_id:
        return request
    return request.model_copy(update={"correlation_id": f"{request.resolved_asset_id}-{digest(seed, 12)}"})


def _object_request(detail: dict[str, Any], default_ladder_version: str) -> IngestRequest:
    bucket, key = detail["bucket"]["name"], detail["object"]["key"]
    sidecar = _sidecar(bucket, key)
    if forbidden := sorted({"source_uri", "generator"} & sidecar.keys()):
        raise InvalidEventError(f"sidecar {key}{SIDECAR_SUFFIX} must not set {', '.join(forbidden)}")
    fields = {"ladder_version": default_ladder_version, **sidecar, "source_uri": f"s3://{bucket}/{key}"}
    if not fields.get("cue_points_ms") and (offsets := _tagged_offsets(bucket, key)):
        fields["cue_points_ms"] = offsets
    return IngestRequest.model_validate(fields)


def _sidecar(bucket: str, key: str) -> dict[str, Any]:
    body = S3Store(bucket).read(f"{key}{SIDECAR_SUFFIX}")
    if body is None:
        return {}
    try:
        sidecar = json.loads(body)
    except json.JSONDecodeError as error:
        raise InvalidEventError(f"sidecar {key}{SIDECAR_SUFFIX} is not valid JSON: {error}") from None
    if not isinstance(sidecar, dict):
        raise InvalidEventError(f"sidecar {key}{SIDECAR_SUFFIX} must be a JSON object")
    return sidecar


def _tagged_offsets(bucket: str, key: str) -> list[int]:
    tags = aws.client("s3").get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
    value = next((t["Value"] for t in tags if t["Key"].lower().replace("_", "") == AD_OFFSETS_TAG), "")
    try:
        return [round(float(token)) for token in re.split(r"[\s,]+", value.strip()) if token]
    except ValueError:
        raise InvalidEventError(f"tag AdOffsets={value!r} is not a list of milliseconds") from None


def _sequencer(event: dict[str, Any]) -> str:
    detail = event["detail"]
    return f"{detail['bucket']['name']}/{detail['object']['key']}#{detail['object'].get('sequencer', event['id'])}"


def _reject(event: dict[str, Any], error: Exception, *, default_ladder_version: str) -> dict[str, str]:
    """Report a request that can never succeed (retrying would not help) as ``Asset Failed``."""
    detail = event.get("detail") or {}
    if event.get("source") == "aws.s3":
        fields = {"source_uri": f"s3://{detail['bucket']['name']}/{detail['object']['key']}"}
        correlation_id = f"rejected-{digest(_sequencer(event), 12)}"
    else:
        fields = detail
        correlation_id = str(detail.get("correlation_id") or f"rejected-{digest(str(event.get('id')), 12)}")
    source = fields.get("source_uri")
    kind = fields.get("kind")
    failed = AssetFailed(
        asset_id=str(fields.get("asset_id") or (asset_id_for(source) if isinstance(source, str) else "unknown")),
        kind=kind if kind in set(AssetKind) else AssetKind.CONTENT,
        ladder_version=str(fields.get("ladder_version") or default_ladder_version),
        error="InvalidIngestRequest",
        cause=str(error),
        correlation_id=correlation_id,
    )
    logger.set_correlation_id(correlation_id)
    logger.error("ingest request rejected", extra={"asset_id": failed.asset_id, "cause": failed.cause})
    put_event(failed)
    return {"status": "REJECTED", "correlation_id": correlation_id}
