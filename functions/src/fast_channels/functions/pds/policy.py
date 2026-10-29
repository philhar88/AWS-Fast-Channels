"""``POST /policy``: the patch the fast-ctv Prebid Server module applies before each auction.

Behind the PDS's internal load balancer (Powertools ``ALBResolver``). The answer comes from
``fast_channels.core.pds.policy.decide`` with the current snapshot (``store``): until viewer identity exists every
session is CONTEXTUAL (a ticket that is missing, malformed or not looked up), probes are SAFE, kids configurations
KIDS. The module waits 50 ms for it; anything but a 200 with a known schema leaves the stored request alone (own book
and house, no third party). Each answer is logged as a ``pds.policy`` decision record, which a subscription carries
into the lake.
"""

import os
import time
from functools import cache
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.event_handler import ALBResolver, Response, content_types
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import ValidationError

from fast_channels.core.pds.models import PolicyRequest
from fast_channels.core.pds.policy import decide
from fast_channels.core.pds.records import policy_record
from fast_channels.functions.pds.store import SnapshotStore
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
app = ALBResolver()


@cache
def store() -> SnapshotStore:
    """The function's snapshot store (one per execution environment)."""
    settings().require("pds_config_bucket")
    return SnapshotStore(settings().pds_config_bucket)


def _json(status: int, body: str) -> Response[str]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


@app.post("/policy")
def policy() -> Response[str]:
    """The answer, a 400 for a request that is not the contract's, or a 500: the module then changes nothing."""
    started = time.perf_counter()
    try:
        request = PolicyRequest.model_validate_json(app.current_event.decoded_body or "")
    except ValidationError as error:
        # Field locations only: a value may be a ticket.
        fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        logger.warning("policy request refused", extra={"fields": fields})
        metrics.add_metric(name="PolicyRefused", unit=MetricUnit.Count, value=1)
        return _json(400, '{"error":"invalid request"}')
    try:
        return _answer(request, started)
    except Exception:
        logger.exception("policy failed")
        metrics.add_metric(name="PolicyErrors", unit=MetricUnit.Count, value=1)
        return _json(500, '{"error":"internal"}')


def _answer(request: PolicyRequest, started: float) -> Response[str]:
    logger.set_correlation_id(request.decision_id)
    snapshot = store().get()
    decision = decide(request, snapshot.allow_list, origin_facing=snapshot.networks)
    body = decision.response.model_dump_json()
    record = policy_record(request, decision, latency_ms=round((time.perf_counter() - started) * 1000, 3))
    logger.info("decision", extra={**record.model_dump(mode="json"), "snapshot": snapshot.version})
    with single_metric(
        name="PolicyDecisions", unit=MetricUnit.Count, value=1, default_dimensions=metrics.default_dimensions
    ) as by_mode:
        by_mode.add_dimension(name="mode", value=decision.response.mode.value)
    if decision.response.device.ip is None and decision.response.device.ipv6 is None:
        metrics.add_metric(name="PolicyNoViewerIp", unit=MetricUnit.Count, value=1)
    return _json(200, body)


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # pragma: no cover - load the snapshot during init, not on a request
    store().load()
