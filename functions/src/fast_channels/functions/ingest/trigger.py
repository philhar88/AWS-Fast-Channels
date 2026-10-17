"""Starts an ingest execution for an EventBridge event, named after the request's correlation id.

Two rules invoke this function asynchronously (retries, then the pipeline's dead-letter queue):

* ``Object Created`` in the ingest bucket, for video file extensions. The request is the object plus its optional
  JSON sidecar ``<key>.json`` (any IngestRequest field except ``source_uri`` and ``generator``; upload it first)
  and, when the sidecar has no cue points, the v2-compatible ``AdOffsets`` object tag (milliseconds separated by
  spaces or commas, e.g. ``30000 90000``). When the sidecar lists no caption files, caption files next to the
  video named ``<stem>.<language>.<ext>`` (``movie.en.scc`` for ``movie.mov``; upload them first too) are its
  provided captions. Keys under the pipeline's own prefixes (``work/``, ``captions/``) are ignored.
* ``Ingest Requested`` on the ``fast-channels`` bus; the detail is an IngestRequest.

The sidecar is read here, before StartExecution, so that its ``correlation_id`` becomes the execution name
(SPEC §9). Without one, the name is derived from the S3 sequencer (or the event id): a redelivered event then
resolves to the same execution instead of starting a second encode.

Execution names are unique for 90 days, so a name that exists already means one of two things. EventBridge
delivers at least once: an event that occurred before the execution started is the one that started it, and is
ignored. A later event is a new request that reuses a correlation id (a CMS re-sending ``Ingest Requested``, a
re-upload with an unchanged sidecar); it cannot start, and is reported as ``Asset Failed`` with
``DuplicateCorrelationId`` so that the integration waiting for an outcome gets one.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.captions import CAPTION_EXTENSIONS, LANGUAGE_PATTERN
from fast_channels.core.events import DETAIL_TYPES, AssetFailed
from fast_channels.core.ingest import RESERVED_PREFIXES, AssetKind, IngestRequest, asset_id_for
from fast_channels.core.naming import digest
from fast_channels.functions import aws
from fast_channels.functions.aws import execution_arn
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
DUPLICATE_ERROR: Final = "DuplicateCorrelationId"

logger = Logger()


class InvalidEventError(ValueError):
    """The event cannot become an ingest request."""


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, str]:
    """Start the ingest execution of ``event``; invalid requests are reported as ``Asset Failed`` events."""
    config = settings()
    config.require("ingest_state_machine_arn", "default_ladder_version")
    if event.get("source") == "aws.s3" and str(event["detail"]["object"]["key"]).startswith(RESERVED_PREFIXES):
        logger.info("pipeline file, not an upload", extra={"key": event["detail"]["object"]["key"]})
        return {"status": "IGNORED"}
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
        return _already_started(event, request, execution_arn(config.ingest_state_machine_arn, name))
    logger.info("ingest started", extra={"execution_arn": execution["executionArn"]})
    return {"status": "STARTED", "correlation_id": name, "execution_arn": execution["executionArn"]}


def _already_started(event: dict[str, Any], request: IngestRequest, arn: str) -> dict[str, str]:
    """Ignore a redelivered event; report a new request that reuses the execution's name (see the module)."""
    name = str(request.correlation_id)
    existing = _describe(arn)
    if existing is None or redelivered(event, existing, request):
        logger.warning("event redelivered; the execution it started serves it", extra={"execution_arn": arn})
        return {"status": "DUPLICATE", "correlation_id": name}
    started = existing["startDate"].astimezone(UTC)
    failed = AssetFailed(
        asset_id=request.resolved_asset_id,
        kind=request.kind,
        ladder_version=request.ladder_version,
        error=DUPLICATE_ERROR,
        cause=f"ingest execution {name} already exists ({existing['status']}, started "
        f"{started:%Y-%m-%dT%H:%M:%SZ}); send the request again with a new correlation_id",
        correlation_id=name,
    )
    logger.error("correlation id reused by a new request", extra={"execution_arn": arn, "cause": failed.cause})
    put_event(failed)
    return {"status": "REJECTED", "correlation_id": name}


def redelivered(event: dict[str, Any], execution: dict[str, Any], request: IngestRequest) -> bool:
    """Whether ``event`` is the one that started ``execution`` (DescribeExecution).

    An event occurs before the execution it starts; EventBridge ``time`` has second precision, so an event that
    started it is never later than ``startDate``. Without a time, an identical input is taken as a redelivery.
    """
    if occurred := event.get("time"):
        return bool(datetime.fromisoformat(str(occurred)) <= execution["startDate"])
    return bool(execution.get("input") == request.model_dump_json(exclude_none=True))


def _describe(arn: str) -> dict[str, Any] | None:
    try:
        return dict(aws.client("stepfunctions").describe_execution(executionArn=arn))
    except ClientError:
        logger.warning("cannot describe the existing execution", extra={"execution_arn": arn}, exc_info=True)
        return None


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
    captions = fields.get("captions") or {}
    if isinstance(captions, dict) and "files" not in captions and (files := caption_siblings(bucket, key)):
        fields["captions"] = captions | {"files": files}
    return IngestRequest.model_validate(fields)


_SIBLING: Final = re.compile(rf"(?P<language>{LANGUAGE_PATTERN[1:-1]})(?P<extension>\.[A-Za-z]+)")


def caption_siblings(bucket: str, key: str) -> list[dict[str, str]]:
    """Caption files next to a video, ``<stem>.<language>.<ext>``, as CaptionFile fields (URIs relative to the
    video's folder), sorted by key."""
    folder, _, name = key.rpartition("/")
    stem = f"{PurePosixPath(name).stem}."
    prefix = f"{folder}/{stem}" if folder else stem
    pages = aws.client("s3").get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    files = []
    for item in (item for page in pages for item in page.get("Contents", [])):
        sibling = str(item["Key"])
        match = _SIBLING.fullmatch(sibling[len(prefix) :])
        if match and match["extension"].lower() in CAPTION_EXTENSIONS:
            files.append({"uri": sibling.rpartition("/")[2], "language": match["language"]})
    if files:
        logger.info("caption files found next to the upload", extra={"captions": files})
    return files


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
