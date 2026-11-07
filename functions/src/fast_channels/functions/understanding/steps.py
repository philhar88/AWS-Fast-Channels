"""The steps of the understanding workflow, which the Understanding stack's state machine runs once per title.

1. :func:`prepare`: from an ``Asset Ready`` event of a content asset, the title (its catalog ``content_id``), the
   analysis proxy's MediaConvert job and the S3 keys; a title that already has an item is skipped (re-analysis is a
   manual start with ``"force": true``).
2. The state machine encodes the proxy, then runs the analyses in parallel (Transcribe, Rekognition Video), each
   started and polled by the state machine itself.
3. :func:`collect`: pages each finished analysis's results, keeps them raw under ``results/`` and reduces them to
   :class:`~fast_channels.core.understanding.signals.Signals`.
4. :func:`describe`: one Bedrock Converse call through the model registry (task ``understanding.describe``), the
   checks on its answer, the ``UNDERSTANDING`` item, and the ``Catalog Item Changed`` and ``Content Understood``
   events.

The workflow fails open: a failed analysis leaves its signals out, a failed or refused model call leaves the
description out, and the item is then written ``partial`` with whatever succeeded.
"""

# OPTIMIZATION(aws): EI-VOD Elemental Inference has no VOD analysis: this workflow stands in for it

import json
import re
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from posixpath import dirname, join, normpath
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict

from fast_channels.core.events import AssetReady, CatalogItemChanged, ContentUnderstood, put_events_entry
from fast_channels.core.ingest import AssetKind
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.model_registry import TaskEntry, model_family, task_parameter
from fast_channels.core.model_spend import CapReached, SpendCounter, cost
from fast_channels.core.understanding.cost import estimate
from fast_channels.core.understanding.prompt import Description, answer, description, request
from fast_channels.core.understanding.proxy import analysis_variant, proxy_job
from fast_channels.core.understanding.record import (
    Analysis,
    Status,
    Understanding,
    item_key,
    stamp,
)
from fast_channels.core.understanding.signals import (
    Signals,
    label_signals,
    moderation_signals,
    segment_signals,
    transcript_signals,
)
from fast_channels.functions import aws
from fast_channels.functions.media_library import asset_key
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

PROXIES: Final = "proxies/"
RESULTS: Final = "results/"
MULTIVARIANT: Final = "main.m3u8"
PAGE_SIZE: Final = 1000
"""Rekognition ``Get*`` results per page (the maximum)."""
MODEL_CONFIG: Final = Config(retries={"mode": "adaptive", "max_attempts": 4}, connect_timeout=5, read_timeout=300)
"""A video model reads up to 960 frames per call, which can take a minute or more."""

logger = Logger()
metrics = Metrics()


class Prepared(BaseModel):
    """What :func:`prepare` hands the state machine (and, through it, the later steps)."""

    model_config = ConfigDict(frozen=True)

    skip: bool
    reason: str = ""
    content_id: str = ""
    asset_id: str = ""
    duration_ms: int = 0
    correlation_id: str = ""
    bucket: str = ""
    proxy_key: str = ""
    """``proxies/<content_id>/<asset_id>/proxy.mp4``."""
    results: str = ""
    """``results/<content_id>/<asset_id>/``: the analyses' raw results and the signals."""
    transcript_key: str = ""
    transcription_job: str = ""
    token: str = ""
    """Idempotency token of the Rekognition jobs (``ClientRequestToken``)."""
    job: dict[str, Any] = {}
    """The proxy's MediaConvert ``Settings`` and ``UserMetadata``."""


# --- 1. prepare ----------------------------------------------------------------------------------------------------


@logger.inject_lambda_context
def prepare(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The analysis plan of the asset in ``event["detail"]`` (an ``Asset Ready`` detail), or ``skip``."""
    cfg = settings()
    cfg.require("library_table", "catalog_table", "media_bucket", "understanding_bucket")
    ready = AssetReady.model_validate(event["detail"])
    execution = str(event.get("execution") or context.aws_request_id)
    logger.append_keys(asset_id=ready.asset_id, correlation_id=ready.correlation_id)
    if ready.kind is not AssetKind.CONTENT:
        return _skip(f"a {ready.kind} asset is not a title")
    asset = aws.table(cfg.library_table).get_item(Key=asset_key(ready.asset_id, ready.ladder_version)).get("Item")
    content_id = str((asset or {}).get("content_id") or "")
    if not content_id:
        return _skip("the asset names no catalog title (content_id)")
    logger.append_keys(content_id=content_id)
    existing = aws.table(cfg.catalog_table).get_item(Key=item_key(content_id)).get("Item")
    if existing and not event.get("force"):
        return _skip(f"the title was analysed already, from asset {existing.get('asset_id')!s}")
    prefix = output_prefix(ready.asset_id, ready.ladder_version)
    multivariant = aws.client("s3").get_object(Bucket=cfg.media_bucket, Key=f"{prefix}{MULTIVARIANT}")
    variant = analysis_variant(multivariant["Body"].read().decode())
    variant_key = normpath(join(dirname(f"{prefix}{MULTIVARIANT}"), variant))
    run = f"{content_id}/{ready.asset_id}"
    job = proxy_job(
        input_uri=f"s3://{cfg.media_bucket}/{variant_key}",
        destination=f"s3://{cfg.understanding_bucket}/{PROXIES}{run}/",
        metadata={"app": cfg.prefix, "content_id": content_id, "asset_id": ready.asset_id, "execution": execution},
    )
    prepared = Prepared(
        skip=False,
        content_id=content_id,
        asset_id=ready.asset_id,
        duration_ms=ready.duration_ms,
        correlation_id=execution,
        bucket=cfg.understanding_bucket,
        proxy_key=f"{PROXIES}{run}/proxy.mp4",
        results=f"{RESULTS}{run}/",
        transcript_key=f"{RESULTS}{run}/transcript.json",
        transcription_job=f"{cfg.prefix}-{_safe(execution)}"[:200],
        token=_safe(execution)[:56],
        job=job,
    )
    logger.info("title to analyse", extra={"variant": variant_key, "duration_ms": ready.duration_ms})
    return prepared.model_dump(mode="json")


def _skip(reason: str) -> dict[str, Any]:
    logger.info("nothing to analyse", extra={"reason": reason})
    return Prepared(skip=True, reason=reason).model_dump(mode="json")


def _safe(name: str) -> str:
    """``name`` with only the characters Transcribe job names and Rekognition request tokens both take."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


# --- 3. collect ----------------------------------------------------------------------------------------------------


@logger.inject_lambda_context
def collect(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The signals of the analyses that finished, from the state machine's ``{"prepared": ..., "analyses": [...]}``:
    the Parallel state's output, a list per branch of ``{"analysis", "job_id"}`` or ``{"analysis", "error"}`` (shots
    and technical cues share one segment-detection job, read once)."""
    prepared = Prepared.model_validate(event["prepared"])
    logger.append_keys(content_id=prepared.content_id, asset_id=prepared.asset_id)
    found: dict[str, Any] = {}
    read: dict[str, dict[str, Any]] = {}
    done: list[Analysis] = []
    failed: list[str] = []
    for run in _runs(event.get("analyses") or []):
        analysis = Analysis(run["analysis"])
        if run.get("error") or not (run.get("job_id") or analysis is Analysis.TRANSCRIPT):
            failed.append(analysis.value)
            logger.warning("analysis failed", extra={"analysis": analysis.value, "error": run.get("error")})
            continue
        job = f"{run.get('job_id')}"
        try:
            if job not in read:
                read[job] = _results(analysis, prepared, run)
        except (ClientError, BotoCoreError, ValueError, KeyError) as error:
            failed.append(analysis.value)
            logger.warning("analysis results unreadable", extra={"analysis": analysis.value, "error": str(error)})
            continue
        found |= read[job]
        done.append(analysis)
    signals = Signals(duration_ms=prepared.duration_ms, analyses=tuple(done), **found)
    aws.client("s3").put_object(
        Bucket=prepared.bucket,
        Key=f"{prepared.results}signals.json",
        Body=signals.model_dump_json(indent=1).encode(),
        ContentType="application/json",
    )
    return {"signals": signals.model_dump(mode="json"), "failed": failed}


def _runs(branches: list[Any]) -> Iterator[Mapping[str, Any]]:
    """The analyses' entries, from the Parallel state's output: one list of entries per branch."""
    for branch in branches:
        yield from branch if isinstance(branch, list) else [branch]


def _results(analysis: Analysis, prepared: Prepared, run: Mapping[str, Any]) -> dict[str, Any]:
    """One analysis's signals (and its raw results kept under ``results/``)."""
    rekognition = aws.client("rekognition")
    match analysis:
        case Analysis.TRANSCRIPT:
            body = aws.client("s3").get_object(Bucket=prepared.bucket, Key=prepared.transcript_key)["Body"].read()
            return transcript_signals(json.loads(body), prepared.duration_ms)
        case Analysis.SHOTS | Analysis.TECHNICAL_CUES:
            segments = _keep(prepared, "segments", _pages(rekognition.get_segment_detection, run["job_id"], "Segments"))
            return segment_signals(segments)
        case Analysis.LABELS:
            labels = _keep(
                prepared,
                "labels",
                _pages(rekognition.get_label_detection, run["job_id"], "Labels", AggregateBy="SEGMENTS"),
            )
            return {"labels": label_signals(labels)}
        case Analysis.MODERATION:
            moderation = _keep(
                prepared, "moderation", _pages(rekognition.get_content_moderation, run["job_id"], "ModerationLabels")
            )
            return {"moderation": moderation_signals(moderation)}


def _pages(call: Callable[..., Mapping[str, Any]], job_id: str, key: str, **extra: Any) -> Iterator[dict[str, Any]]:
    """Every entry of a Rekognition ``Get*`` job result, page by page."""
    request: dict[str, Any] = {"JobId": job_id, "MaxResults": PAGE_SIZE, **extra}
    while True:
        page = call(**request)
        if page.get("JobStatus") != "SUCCEEDED":
            raise ValueError(f"job {job_id} is {page.get('JobStatus')}: {page.get('StatusMessage', '')}")
        yield from page.get(key) or []
        if not (token := page.get("NextToken")):
            return
        request["NextToken"] = token


def _keep(prepared: Prepared, name: str, entries: Iterator[dict[str, Any]]) -> list[dict[str, Any]]:
    """``entries`` as a list, also written to ``results/.../<name>.json``."""
    kept = list(entries)
    aws.client("s3").put_object(
        Bucket=prepared.bucket,
        Key=f"{prepared.results}{name}.json",
        Body=json.dumps(kept, default=str).encode(),
        ContentType="application/json",
    )
    return kept


# --- 4. describe ---------------------------------------------------------------------------------------------------


@metrics.log_metrics
@logger.inject_lambda_context
def describe(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The model's description of the title, the ``UNDERSTANDING`` item and the events, from
    ``{"prepared": ..., "signals": ..., "failed": [...]}``."""
    cfg = settings()
    cfg.require("catalog_table", "event_bus", "understanding_task")
    prepared = Prepared.model_validate(event["prepared"])
    signals = Signals.model_validate(event["signals"])
    failed = list(event.get("failed") or [])
    logger.append_keys(content_id=prepared.content_id, asset_id=prepared.asset_id)
    owner = context.invoked_function_arn.split(":")[4]
    entry, described, usd = None, None, Decimal(0)
    try:
        entry = registry_entry(cfg.prefix, cfg.understanding_task)
        described, usd = _describe(entry, prepared, signals, task=cfg.understanding_task, bucket_owner=owner)
    except CapReached as error:
        logger.warning("model spend cap reached: the item has no description", extra={"cap": error.message})
    except (ClientError, BotoCoreError, ValueError) as error:
        logger.warning("the describe step failed: the item has no description", extra={"error": str(error)})
    model = model_family(entry.model) if entry is not None and described is not None else None
    item = _item(prepared, signals, described, model=model, failed=failed)
    aws.table(cfg.catalog_table).put_item(Item={**item_key(prepared.content_id), **_dynamo(item)})
    _emit(prepared, item.status)
    metrics.add_metric(name="TitlesUnderstood", unit=MetricUnit.Count, value=1)
    if item.status is Status.PARTIAL:
        metrics.add_metric(name="TitlesUnderstoodPartially", unit=MetricUnit.Count, value=1)
    estimated = estimate(prepared.duration_ms, signals.analyses)
    metrics.add_metric(name="UnderstandingCostUsd", unit=MetricUnit.Count, value=float(estimated))
    logger.info(
        "title understood",
        extra={
            "status": item.status.value,
            "failed": failed,
            "cat": list(item.cat),
            "brand_safety": item.brand_safety.value if item.brand_safety else None,
            "model_usd": str(usd),
            "estimated_usd": str(estimated),
            "dropped": list(described.dropped) if described else [],
        },
    )
    return {"status": item.status.value, "content_id": prepared.content_id, "estimated_usd": str(estimated)}


@cache
def registry_entry(prefix: str, task: str) -> TaskEntry:
    """The task's registry entry (``/<prefix>/models/tasks/<task>``), validated."""
    raw = aws.client("ssm").get_parameter(Name=task_parameter(prefix, task))["Parameter"]["Value"]
    return TaskEntry.model_validate(json.loads(raw))


def _describe(
    entry: TaskEntry, prepared: Prepared, signals: Signals, *, task: str, bucket_owner: str
) -> tuple[Description, Decimal]:
    """One Converse call under the task's daily cap; the checked description and what the call cost."""
    cfg = settings()
    counter = SpendCounter(
        table=cfg.models_counter_table,
        topic_arn=cfg.alert_topic_arn or None,
        dynamodb=aws.client("dynamodb"),
        sns=aws.client("sns"),
    )
    if entry.daily_cap_usd is not None:
        counter.admit(task, Decimal(str(entry.daily_cap_usd)))
    response = _bedrock().converse(
        **request(
            model_id=entry.profile_arn,
            video_uri=f"s3://{prepared.bucket}/{prepared.proxy_key}",
            bucket_owner=bucket_owner,
            signals=signals,
            max_tokens=entry.max_tokens,
        )
    )
    usd = cost(entry.model, response.get("usage") or {})
    counter.add(task, usd)
    logger.info("model answered", extra={"usage": response.get("usage"), "stop_reason": response.get("stopReason")})
    return description(answer(response), moderation_tier=signals.moderation_tier), usd


@cache
def _bedrock() -> Any:
    return boto3.client("bedrock-runtime", config=MODEL_CONFIG)


def _item(
    prepared: Prepared, signals: Signals, described: Description | None, *, model: str | None, failed: list[str]
) -> Understanding:
    complete = described is not None and not failed and set(signals.analyses) >= set(_configured())
    return Understanding(
        content_id=prepared.content_id,
        asset_id=prepared.asset_id,
        status=Status.COMPLETE if complete else Status.PARTIAL,
        analyses=signals.analyses,
        cat=described.cat if described else (),
        keywords=described.keywords if described else (),
        moods=described.moods if described else (),
        brand_safety=described.brand_safety if described else signals.moderation_tier,
        moderation=tuple(signals.moderation),
        synopsis=described.synopsis if described else "",
        language=signals.language,
        shots=signals.shots,
        speech_ratio=signals.speech_ratio,
        duration_ms=prepared.duration_ms,
        model=model,
        analyzed_at=datetime.now(UTC),
    )


def _configured() -> tuple[Analysis, ...]:
    return tuple(Analysis(a) for a in settings().understanding_analyses)


def _dynamo(item: Understanding) -> dict[str, Any]:
    """The item's attributes as DynamoDB takes them (floats as Decimals, the timestamp as text)."""
    document = item.model_dump(mode="json")
    document["analyzed_at"] = stamp(item.analyzed_at)
    attributes: dict[str, Any] = json.loads(json.dumps(document), parse_float=Decimal)
    return attributes


def _emit(prepared: Prepared, status: Status) -> None:
    """``Catalog Item Changed`` (crawl republishes the title) and ``Content Understood``."""
    cfg = settings()
    entries = [
        put_events_entry(
            CatalogItemChanged(content_id=prepared.content_id, correlation_id=prepared.correlation_id),
            prefix=cfg.prefix,
            event_bus=cfg.event_bus,
        ),
        put_events_entry(
            ContentUnderstood(
                content_id=prepared.content_id,
                asset_id=prepared.asset_id,
                status=status.value,
                correlation_id=prepared.correlation_id,
            ),
            prefix=cfg.prefix,
            event_bus=cfg.event_bus,
        ),
    ]
    response = aws.client("events").put_events(Entries=entries)
    if response.get("FailedEntryCount"):
        logger.warning("events not sent", extra={"entries": response.get("Entries")})
