"""``POST /select``: the pod the fast-ctv Prebid Server module renders after each auction.

Behind the PDS's internal load balancer (Powertools ``ALBResolver``). The answer comes from
``fast_channels.core.pds.selection.select``:
- the pod rules run on the current snapshot (the own book's tier-0 deals, the floor, the break of record);
- every own-book line ad in the pod or among its standalones is reserved first (``reservations.reserve``, on the shard
  ``budget.Budgets.shard`` picks), and a line whose price cannot be reserved is left out.

Each ad carries our impression tracker on the CDN (``core.pds.pod.tracker_url``). The module waits 100 ms for the
answer; anything but a 200 with a known schema makes it render the auction's house ads (``SELECT_FALLBACK``). Each
answer is logged as a ``pds.select`` decision record, which a subscription carries into the lake.
"""

import os
import time
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.event_handler import ALBResolver, Response, content_types
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.pbs_config import TRACKER_PATH
from fast_channels.core.pds.models import SelectRequest
from fast_channels.core.pds.names import Endpoint
from fast_channels.core.pds.pod import select_response
from fast_channels.core.pds.records import select_record
from fast_channels.core.pds.selection import pod_context, scheduled_ms, select
from fast_channels.functions.pds.budget import Budgets
from fast_channels.functions.pds.reservations import Hold, reserve
from fast_channels.functions.pds.store import CLIENT_CONFIG, snapshot_store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_lambda_powertools.utilities.typing import LambdaContext

    from fast_channels.core.pds.book import Line

logger = Logger()
metrics = Metrics()
app = ALBResolver()

store = snapshot_store
"""The function's snapshot store (one per execution environment)."""
ENR = 0
"""The content-enrichment arm of every break until the crawl module enriches ``app.content`` (M17-04)."""


@cache
def budgets() -> Budgets:
    """The lines' budgets, for the lines of the snapshot the function has."""
    settings().require("pds_table")
    return Budgets(settings().pds_table, lines=lambda: store().current.lines)


@cache
def dynamodb() -> Any:
    """The reservations' client: short timeouts, since the module waits 100 ms for the whole answer."""
    return boto3.client("dynamodb", config=CLIENT_CONFIG)


@cache
def tracker_base() -> str:
    """Our impression tracker on the CDN: ``<content base URL>/ads/b.gif``."""
    settings().require("content_base_url")
    return f"{settings().content_base_url.rstrip('/')}{TRACKER_PATH}"


def _json(status: int, body: str) -> Response[str]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body)


@app.post(Endpoint.SELECT.value)
def selection() -> Response[str]:
    """The pod, a 400 for a request that is not the contract's, or a 500: the module then renders house ads."""
    started = time.perf_counter()
    try:
        request = SelectRequest.model_validate_json(app.current_event.decoded_body or "")
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        logger.warning("select request refused", extra={"fields": fields})
        metrics.add_metric(name="SelectRefused", unit=MetricUnit.Count, value=1)
        return _json(400, '{"error":"invalid request"}')
    try:
        return _answer(request, started)
    except Exception:
        logger.exception("select failed")
        metrics.add_metric(name="SelectErrors", unit=MetricUnit.Count, value=1)
        return _json(500, '{"error":"internal"}')


def _reserver(request: SelectRequest) -> Any:
    """The reservation step of ``selection.select`` for this break: refuses a line no shard can take, and every line
    when DynamoDB fails (no line ad is answered unreserved)."""

    def reserve_lines(wanted: Mapping[Line, tuple[str, ...]]) -> frozenset[str]:
        settings().require("pds_table", "ledger_table")
        view = budgets()
        holds: list[Hold] = []
        refused: set[str] = set()
        for line, crids in wanted.items():
            shard = view.shard(line, request.decision_id, line.insertion * len(crids))
            if shard is None:
                refused.update(crids)
            else:
                holds.append(Hold(line=line, crids=crids, shard=shard))
        if not holds:
            return frozenset(refused)
        try:
            done = reserve(
                dynamodb(),
                table=settings().pds_table,
                ledger_table=settings().ledger_table,
                decision=request.decision_id,
                sid=request.sid,
                holds=holds,
                now=datetime.now(UTC),
            )
        except (BotoCoreError, ClientError) as error:
            logger.warning("reservation failed", extra={"error": type(error).__name__, "detail": str(error)[:300]})
            metrics.add_metric(name="ReservationErrors", unit=MetricUnit.Count, value=1)
            return frozenset(refused | {crid for hold in holds for crid in hold.crids})
        return frozenset(refused | set(done.refused))

    return reserve_lines


def _answer(request: SelectRequest, started: float) -> Response[str]:
    logger.set_correlation_id(request.decision_id)
    snapshot = store().get()
    rules = pod_context(
        snapshot.book, floor_cpm=snapshot.floor_cpm, scheduled=scheduled_ms(request.dur_ms, snapshot.breaks_ms)
    )
    decision, reserved = select(request, rules, snapshot.book, reserve=_reserver(request))
    body = select_response(request, decision, tracker_base=tracker_base(), enr=ENR).model_dump_json()
    record = select_record(
        request, decision, latency_ms=round((time.perf_counter() - started) * 1000, 3), reserved=reserved
    )
    logger.info("decision", extra={**record.model_dump(mode="json"), "snapshot": snapshot.version})
    with single_metric(
        name="SelectDecisions", unit=MetricUnit.Count, value=1, default_dimensions=metrics.default_dimensions
    ) as by_reason:
        by_reason.add_dimension(name="reason", value=decision.reason.value)
    metrics.add_metric(name="LineAdsReserved", unit=MetricUnit.Count, value=len(reserved))
    return _json(200, body)


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # pragma: no cover - read both during init, not on a request
    store().load()
    budgets().load()
