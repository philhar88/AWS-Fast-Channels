"""``Captions``: finish resolving an asset's caption tracks after Prepare (``core.captions``).

Prepare resolves provided files and Probe-visible tracks. When the spoken language still has no source, the state
machine runs the caption scan job and then this step:

* ``scanned``: count the cues the scan found in the video (CEA-608/708). Captions found are read from the source
  by the encode itself; otherwise the plan awaits generation, and the result carries the captioner's input.
* ``generated``: the captioner wrote the spoken language's captions; check the file and use it.
* ``failed``: the captioner failed; the track stays empty, unless the stage requires captions.

Every step returns the asset context with the plan and, once nothing is awaited, the job's caption selectors.

The captioner contract (any state machine that honours it can generate captions):

* input ``{"asset_id", "language", "source_uri", "output_uri", "correlation_id"}``: ``source_uri`` is the mezzanine
  (the captioner prepares what its engine needs from it, under ``work/``), ``output_uri`` an ``s3://`` URI stem in
  the ingest bucket that the captioner adds an extension to;
* output ``{"uri", "format", "engine"}``: the captions file, its ``core.captions.CaptionFormat`` and the engine's
  name. A failure (any error) leaves the track empty.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.captions import (
    CaptionFormat,
    CaptionPlan,
    CaptionPolicy,
    CaptionsRefusedError,
    after_generation,
    after_scan,
    generation_failed,
)
from fast_channels.core.ingest import CAPTIONS_PREFIX
from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import count_cues
from fast_channels.functions.ingest.prepare import IngestRefusedError
from fast_channels.functions.ingest.publish import summarize_cause
from fast_channels.functions.ingest.storage import S3Store, read_text
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()


class CaptionsMissingError(RuntimeError):
    """A file the captions were supposed to be in is missing or empty."""


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """``{"step": "scanned" | "generated" | "failed", "asset": <asset context>, ...}`` -> the asset context."""
    config = settings()
    config.require("ingest_bucket")
    asset = event["asset"]
    logger.set_correlation_id(asset["correlation_id"])
    logger.append_keys(asset_id=asset["asset_id"], ladder_version=asset["ladder_version"])
    plan = CaptionPlan.model_validate(asset["captions"])
    policy = CaptionPolicy(required=config.captions_required, can_generate=bool(config.captioner))
    store = S3Store(config.ingest_bucket)
    try:
        match event["step"]:
            case "scanned":
                plan = scanned(store, plan, asset["caption_scan"]["captions_uri"], policy)
            case "generated":
                plan = generated(store, plan, event["result"], policy)
            case "failed":
                error = event.get("error") or {}
                reason = f"{error.get('Error', 'States.Unknown')}: {summarize_cause(error.get('Cause'))}"
                logger.warning("caption generation failed", extra={"error": reason})
                metrics.add_metric(name="CaptionGenerationFailed", unit=MetricUnit.Count, value=1)
                plan = generation_failed(plan, reason, policy)
            case step:
                raise ValueError(f"unknown step {step!r}")
    except CaptionsRefusedError as error:
        raise IngestRefusedError(str(error)) from None
    return with_plan(asset, plan)


def scanned(store: S3Store, plan: CaptionPlan, captions_uri: str, policy: CaptionPolicy) -> CaptionPlan:
    """The plan after the caption scan wrote ``captions_uri`` (a TTML of CC1, without cues when there is none)."""
    text = read_text(store, _key(captions_uri, store))
    if text is None:
        raise CaptionsMissingError(f"the caption scan wrote no {captions_uri}")
    cues = count_cues(text)
    metrics.add_metric(name="CaptionScans", unit=MetricUnit.Count, value=1)
    logger.info("caption scan", extra={"embedded_cues": cues})
    return after_scan(plan, cues, policy)


def generated(store: S3Store, plan: CaptionPlan, result: Mapping[str, Any], policy: CaptionPolicy) -> CaptionPlan:
    """The plan once the captioner reported ``result`` (``{"uri", "format", "engine"}``)."""
    uri, engine = str(result["uri"]), str(result["engine"])
    body = store.read(_key(uri, store))
    if not body:
        raise CaptionsMissingError(f"the captioner {engine} reported {uri}, which is missing or empty")
    metrics.add_metric(name="CaptionsGenerated", unit=MetricUnit.Count, value=1)
    logger.info("captions generated", extra={"uri": uri, "engine": engine, "bytes": len(body)})
    return after_generation(plan, uri=uri, format=CaptionFormat(result["format"]), engine=engine, policy=policy)


def with_plan(asset: Mapping[str, Any], plan: CaptionPlan) -> dict[str, Any]:
    """``asset`` with ``plan``; its job's caption selectors once the plan is final, else the captioner's input."""
    updated = deepcopy(dict(asset)) | {"captions": plan.model_dump(mode="json")}
    if plan.awaiting == "generation":
        updated["captioner_input"] = captioner_input(asset, plan)
    elif plan.awaiting is None:
        ladder = Ladder.model_validate(asset["ladder"])
        updated["job"]["Settings"]["Inputs"][0]["CaptionSelectors"] = plan.selectors(ladder.captions)
        updated.pop("captioner_input", None)
    return updated


def captioner_input(asset: Mapping[str, Any], plan: CaptionPlan) -> dict[str, str]:
    """The captioner contract's input for the spoken-language track of ``asset``."""
    if plan.spoken is None:
        raise ValueError("the ladder has no caption track for the spoken language")
    asset_id, correlation_id = asset["asset_id"], asset["correlation_id"]
    stem = f"s3://{settings().ingest_bucket}/{CAPTIONS_PREFIX}{asset_id}/{correlation_id}/{plan.spoken.language}"
    return {
        "asset_id": asset_id,
        "language": plan.spoken.language,
        "source_uri": asset["source_uri"],
        "output_uri": stem,
        "correlation_id": correlation_id,
    }


def _key(uri: str, store: S3Store) -> str:
    parts = urlsplit(uri)
    if parts.scheme != "s3" or parts.netloc != store.bucket:
        raise CaptionsMissingError(f"{uri} is not in the ingest bucket {store.bucket}")
    return parts.path.lstrip("/")
