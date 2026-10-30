"""``POST /policy``: the patch the fast-ctv Prebid Server module applies before each auction.

The answer comes from ``fast_channels.core.pds.policy.decide`` with the current snapshot (``state.store``): until
viewer identity exists every session is CONTEXTUAL (a ticket that is missing, malformed or not looked up), probes are
SAFE, kids configurations KIDS. With the crawl module on, its patch contributor (``functions.crawl.patch``) adds the
title's ``app.content``. The module waits ``modules.pds.policy_timeout_ms`` for the answer; anything but a 200 with a
known schema leaves the stored request alone (own book and house, no third party). Before the sidecar has a snapshot
it answers 503 at once (``http.NO_SNAPSHOT``). Each answer is logged as a ``pds.policy`` decision record, which a
subscription carries into the lake.
"""

import time
from typing import TYPE_CHECKING

from aws_lambda_powertools import Logger
from pydantic import ValidationError

from fast_channels.core.pds.models import PolicyRequest
from fast_channels.core.pds.policy import decide
from fast_channels.core.pds.records import policy_record
from fast_channels.core.pds.snapshot import EMPTY
from fast_channels.functions.crawl.patch import contributor
from fast_channels.pds import state
from fast_channels.pds.http import INTERNAL, NO_SNAPSHOT, REFUSED, Answer, refused_fields
from fast_channels.pds.telemetry import counters

if TYPE_CHECKING:
    from fast_channels.core.pds.contributors import PatchContributor

logger = Logger(child=True)

store = state.store
"""The sidecar's snapshot store."""


def contributors() -> tuple[PatchContributor, ...]:
    """The patch contributors of the deployed modules: the crawl module's when it is on."""
    crawl = contributor()
    return (crawl,) if crawl is not None else ()


def answer(body: bytes, started: float) -> Answer:
    """The patch, a 400 for a request that is not the contract's, a 503 without a snapshot, or a 500: the module
    then changes nothing. ``started``: when the request arrived (``time.perf_counter``)."""
    try:
        request = PolicyRequest.model_validate_json(body)
    except ValidationError as error:
        logger.warning("policy request refused", extra={"fields": refused_fields(error)})
        counters.add("PolicyRefused")
        return REFUSED
    try:
        return _answer(request, started)
    except Exception:
        logger.exception("policy failed", extra={"correlation_id": request.decision_id})
        counters.add("PolicyErrors")
        return INTERNAL


def _answer(request: PolicyRequest, started: float) -> Answer:
    snapshot = store().get()
    if snapshot.version == EMPTY.version:
        counters.add("PolicyNoSnapshot")
        return NO_SNAPSHOT
    decision = decide(request, snapshot.allow_list, origin_facing=snapshot.networks, contributors=contributors())
    body = decision.response.model_dump_json()
    record = policy_record(request, decision, latency_ms=round((time.perf_counter() - started) * 1000, 3))
    logger.info(
        "decision",
        extra={**record.model_dump(mode="json"), "snapshot": snapshot.version, "correlation_id": request.decision_id},
    )
    counters.add("PolicyDecisions", dimension=("mode", decision.response.mode.value))
    if decision.response.device.ip is None and decision.response.device.ipv6 is None:
        counters.add("PolicyNoViewerIp")
    return Answer(200, body)
