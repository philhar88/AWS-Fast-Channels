"""Steps of the LadderProvisioner state machine: a ladder version's kit (SPEC §5.2).

A kit is what a channel on a ladder needs besides its programs, encoded with that same ladder, because Channel
Assembly and MediaTailor require one rendition set across programs, slates and ads (channel-assembly.md §3.2,
_review.md "agent ladder customisation"): the filler slate, an ad-break slate per ``BREAK_SLATE_GOPS`` length
(a break lasts as long as its slate) and the house ads (``HOUSE_AD_GOPS``) behind a static VAST pod.

* ``begin``: record the ladder version as PROVISIONING (keeping any previous kit until the new one is ready) and
  return the IngestRequests of its kit, which the state machine runs as child executions.
* ``ready``: write the VAST pod ``ads/<ladder>/vast.xml`` and the ``ads/b.gif`` beacon (and, with demo creatives,
  their index ``ads/<ladder>/demo/creatives.json``), mark the ladder READY with its kit and publish ``Ladder Ready``.

With the demand module in demo mode the kit also holds the demo creatives (``fast_channels.core.demo_demand``): one
``DEMO_AD_GOPS`` ad per brand still, which the bidder simulator and the own book's synthetic direct lines serve.
* ``failed``: mark the ladder FAILED and publish ``Ladder Failed``.
"""

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import BaseModel, ValidationError

from fast_channels.core.cost import encode_cost_per_minute
from fast_channels.core.demo_demand import DEMO_CREATIVES, demo_index, demo_index_key
from fast_channels.core.events import LadderFailed, LadderReady
from fast_channels.core.ingest import AssetKind, IngestRequest
from fast_channels.core.ladder import Ladder
from fast_channels.core.mediaconvert import GeneratorInput
from fast_channels.core.naming import digest
from fast_channels.core.specs import Creative, vast_key, vast_pod
from fast_channels.core.tags import Owner
from fast_channels.core.timeline import BREAK_SLATE_GOPS, DEMO_AD_GOPS, HOUSE_AD_GOPS
from fast_channels.functions import aws
from fast_channels.functions.ingest.notify import put_event
from fast_channels.functions.ingest.publish import summarize_cause
from fast_channels.functions.ingest.storage import S3Store
from fast_channels.functions.media_library import LadderStatus, ladder_index, ladder_key, now_iso, update_request
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

FILLER_GOPS: Final = 15
"""Filler slate length (~30 s); Channel Assembly repeats it for as long as the schedule has a gap."""
FILLER_LABEL: Final = "filler"
BEACON_KEY: Final = "ads/b.gif"
BEACON_GIF: Final = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
    b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
"""1x1 transparent GIF that VAST impressions and quartiles hit, so they appear in the CDN logs (e2e.md §7)."""
VAST_CACHE_CONTROL: Final = "max-age=300"
BEACON_CACHE_CONTROL: Final = "no-store"  # every beacon must reach the CDN log
MAX_NAME: Final = 80  # Step Functions execution names (and correlation ids)

logger = Logger()
metrics = Metrics()


class KitImages(BaseModel):
    """Brand stills of the kit (S3 URIs of 1920x1080 PNGs), in the order the house ads use ``HOUSE_AD_GOPS``, and the
    demo creatives' stills by creative id (demand demo mode only)."""

    filler: str
    break_slate: str
    house_ads: tuple[str, ...]
    demo: dict[str, str] = {}


def break_label(gops: int) -> str:
    return f"break-{gops}"


def house_ad_label(number: int) -> str:
    return f"house-ad-{number}"


def demo_label(crid: str) -> str:
    return f"demo-{crid}"


def child_name(parent: str, label: str) -> str:
    """Execution name (= correlation id) of a kit child: ``<parent>-<label>``, shortened with a hash to 80 chars."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", parent)
    name = f"{safe}-{label}"
    if len(name) <= MAX_NAME:
        return name
    return f"{safe[: MAX_NAME - len(label) - 8].rstrip('-_')}-{digest(parent)}-{label}"


def kit_requests(ladder_version: str, images: KitImages, correlation_id: str) -> list[IngestRequest]:
    """The ingest requests of a kit: filler, break slates, house ads, then the demo creatives (if any)."""
    if len(images.house_ads) != len(HOUSE_AD_GOPS):
        raise ValueError(f"{len(HOUSE_AD_GOPS)} house-ad images are required, got {len(images.house_ads)}")
    if images.demo and (missing := sorted({c.crid for c in DEMO_CREATIVES} - images.demo.keys())):
        raise ValueError(f"demo stills missing for {missing}")
    stills = [
        (FILLER_LABEL, images.filler, FILLER_GOPS, AssetKind.SLATE),
        *((break_label(gops), images.break_slate, gops, AssetKind.SLATE) for gops in BREAK_SLATE_GOPS),
        *(
            (house_ad_label(n), image, gops, AssetKind.AD)
            for n, (image, gops) in enumerate(zip(images.house_ads, HOUSE_AD_GOPS, strict=True), start=1)
        ),
        *((demo_label(c.crid), images.demo[c.crid], DEMO_AD_GOPS, AssetKind.AD) for c in DEMO_CREATIVES if images.demo),
    ]
    return [
        IngestRequest(
            generator=GeneratorInput(image_s3_uri=image, duration_gops=gops, label=label),
            kind=kind,
            ladder_version=ladder_version,
            correlation_id=child_name(correlation_id, label),
        )
        for label, image, gops, kind in stills
    ]


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Dispatch on ``event["step"]``: ``begin``, ``ready`` or ``failed``."""
    settings().require("library_table")
    match event["step"]:
        case "begin":
            return begin(event["input"], event["execution"], KitImages.model_validate(event["images"]))
        case "ready":
            return ready(event["kit"], event["assets"])
        case "failed":
            return failed(event["input"], event["execution"], event.get("error") or {})
        case step:
            raise ValueError(f"unknown step {step!r}")


def begin(provision: Mapping[str, Any], execution: str, images: KitImages) -> dict[str, Any]:
    """Record the ladder as PROVISIONING; returns the kit context and its ingest requests.

    ``provision`` is the state machine input: ``{"ladder": <Ladder JSON>, "job_template": str,
    "kit_fingerprint"?: str, "owner"?: "iac"|"agent", "correlation_id"?: str}``.
    """
    ladder = Ladder.model_validate(provision["ladder"])
    correlation_id = str(provision.get("correlation_id") or execution)
    logger.set_correlation_id(correlation_id)
    logger.append_keys(ladder_version=ladder.version)
    fingerprint = str(provision.get("kit_fingerprint") or ladder.version)
    top = max(ladder.video, key=lambda r: r.width * r.height)
    values = {
        "ladder_version": ladder.version,
        "status": LadderStatus.PROVISIONING.value,
        **ladder_index(LadderStatus.PROVISIONING, ladder.version),
        "ladder": ladder.model_dump(mode="json"),
        "description": ladder.description,
        "job_template": provision["job_template"],
        "owner": provision.get("owner", Owner.IAC.value),
        "max_height": ladder.max_height,
        "renditions": len(ladder.video),
        "cost_per_minute_usd": encode_cost_per_minute(ladder),
        "correlation_id": correlation_id,
        "provisioning_fingerprint": fingerprint,
        "updated_at": now_iso(),
    }
    aws.table(settings().library_table).update_item(
        **update_request(ladder_key(ladder.version), values, remove=("error",))
    )
    requests = kit_requests(ladder.version, images, correlation_id)
    metrics.add_metric(name="LadderProvisioningStarted", unit=MetricUnit.Count, value=1)
    logger.info("provisioning kit", extra={"assets": [r.correlation_id for r in requests]})
    return {
        "ladder_version": ladder.version,
        "job_template": provision["job_template"],
        "kit_fingerprint": fingerprint,
        "correlation_id": correlation_id,
        "width": top.width,
        "height": top.height,
        "pod_fill_ms": ladder.grid.gop_exact(max(BREAK_SLATE_GOPS)),
        "requests": [r.model_dump(mode="json", exclude_none=True) for r in requests],
    }


def ready(kit: Mapping[str, Any], assets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Write the VAST pod, mark the ladder READY with its kit and announce it; ``assets`` are the children's
    outputs (``ingest.publish.ready``)."""
    config = settings()
    config.require("media_bucket", "content_base_url")
    ladder_version, correlation_id = kit["ladder_version"], kit["correlation_id"]
    logger.set_correlation_id(correlation_id)
    logger.append_keys(ladder_version=ladder_version)
    by_label = {a["label"]: a for a in assets}
    ads = [by_label[house_ad_label(n)] for n in range(1, len(HOUSE_AD_GOPS) + 1)]
    # Longest first; the pod repeats them to cover the longest break slate, each break being a whole number of ads.
    creatives = [
        Creative(
            creative_id=ad["creative"]["creative_id"],
            duration_ms=ad["duration_ms"],
            hls_uri=ad["creative"]["hls_uri"],
            width=kit["width"],
            height=kit["height"],
        )
        for ad in sorted(ads, key=lambda a: -a["duration_ms"])
    ]
    store = S3Store(config.media_bucket)
    key = vast_key(ladder_version)
    store.write(
        key,
        vast_pod(
            creatives=creatives,
            content_base_url=config.content_base_url,
            fill_ms=int(kit.get("pod_fill_ms", 0)),
        ).encode(),
        cache_control=VAST_CACHE_CONTROL,
    )
    store.write(BEACON_KEY, BEACON_GIF, cache_control=BEACON_CACHE_CONTROL)
    demo = {c.crid: by_label[demo_label(c.crid)] for c in DEMO_CREATIVES if demo_label(c.crid) in by_label}
    if demo:
        encoded = {
            crid: (ad["creative"]["creative_id"], ad["creative"]["hls_uri"], ad["duration_ms"])
            for crid, ad in demo.items()
        }
        index = demo_index(ladder_version, encoded, width=kit["width"], height=kit["height"])
        store.write(
            demo_index_key(ladder_version), index.model_dump_json(indent=2).encode(), cache_control=VAST_CACHE_CONTROL
        )
    summary = {
        "filler": by_label[FILLER_LABEL]["vod_source"],
        "break_slates": {str(gops): by_label[break_label(gops)]["vod_source"] for gops in BREAK_SLATE_GOPS},
        "house_ads": [c.creative_id for c in creatives],
        "vast_key": key,
        "vast_url": f"{config.content_base_url.rstrip('/')}/{key}",
        **({"demo_key": demo_index_key(ladder_version)} if demo else {}),
    }
    values = {
        "status": LadderStatus.READY.value,
        **ladder_index(LadderStatus.READY, ladder_version),
        "kit": summary,
        "kit_fingerprint": kit["kit_fingerprint"],
        "updated_at": now_iso(),
    }
    aws.table(settings().library_table).update_item(
        **update_request(ladder_key(ladder_version), values, remove=("error",))
    )
    put_event(
        LadderReady(ladder_version=ladder_version, correlation_id=correlation_id, job_template=kit["job_template"])
    )
    metrics.add_metric(name="LadderReady", unit=MetricUnit.Count, value=1)
    logger.info("kit ready", extra=summary)
    return {"ladder_version": ladder_version, "kit_fingerprint": kit["kit_fingerprint"], **summary}


def failed(provision: Mapping[str, Any], execution: str, error: Mapping[str, Any]) -> dict[str, str]:
    """Mark the ladder FAILED (when its definition is readable) and announce it."""
    correlation_id = str(provision.get("correlation_id") or execution)
    logger.set_correlation_id(correlation_id)
    name, cause = str(error.get("Error") or "States.Unknown"), summarize_cause(error.get("Cause"))
    try:
        ladder_version = Ladder.model_validate(provision.get("ladder")).version
    except ValidationError:
        ladder_version = ""
    logger.error("kit provisioning failed", extra={"ladder_version": ladder_version, "error": name, "cause": cause})
    if ladder_version:
        values = {
            "ladder_version": ladder_version,
            "status": LadderStatus.FAILED.value,
            **ladder_index(LadderStatus.FAILED, ladder_version),
            "error": f"{name}: {cause}",
            "updated_at": now_iso(),
        }
        aws.table(settings().library_table).update_item(**update_request(ladder_key(ladder_version), values))
    put_event(LadderFailed(ladder_version=ladder_version, correlation_id=correlation_id, error=name, cause=cause))
    metrics.add_metric(name="LadderFailed", unit=MetricUnit.Count, value=1)
    return {"error": name, "cause": cause}
