"""``POST /select``: the pod the fast-ctv Prebid Server module renders after each auction.

Behind the PDS's internal load balancer (Powertools ``ALBResolver``). The answer comes from
``fast_channels.core.pds.selection.select``:
- the pod rules run on the current snapshot (the own book's tier-0 deals, the floor, the break of record);
- every own-book line ad in the pod or among its standalones is reserved first (``reservations.reserve``, on the shard
  ``budget.Budgets.shard`` picks), and a line whose price cannot be reserved is left out.

Each ad carries our impression tracker on the CDN (``core.pds.pod.tracker_url``), with the break's content-enrichment
arm (``enr``, ``functions.crawl.patch.arm``: 0 with the crawl module off). The module waits
``modules.pds.select_timeout_ms`` (200 ms) for the answer, of which the internal load balancer takes 20-40 ms to invoke
the function; anything but a 200 with a known schema makes it render the auction's house ads (``SELECT_FALLBACK``). So
the reservations get their own DynamoDB client (100 ms timeouts, one attempt), which the budget view's background reads
keep connected while breaks follow each other. After an idle minute or more DynamoDB has dropped that connection, and
the first reservation reopens it (up to about 110 ms at 1,769 MB, measured). Reservations stop after
:data:`RESERVE_BEFORE_S`: a line not reserved by then is left out, never answered unreserved. A reservation that
DynamoDB committed after the module gave up stays until the sweeper releases it. Each answer is logged as a
``pds.select`` decision record, which a subscription carries into the lake.
"""

import os
import time
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.event_handler import ALBResolver, Response
from aws_lambda_powertools.metrics import MetricUnit
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.pbs_config import TRACKER_PATH
from fast_channels.core.pds.delivery import budget_key
from fast_channels.core.pds.models import SelectRequest
from fast_channels.core.pds.names import Endpoint
from fast_channels.core.pds.pod import scheduled_ms, select_response, tracker_url
from fast_channels.core.pds.records import select_record
from fast_channels.core.pds.selection import Reserve, pod_context, select
from fast_channels.functions.crawl.patch import arm
from fast_channels.functions.pds.budget import Budgets
from fast_channels.functions.pds.http import json_response, refused_fields
from fast_channels.functions.pds.reservations import Hold, item_key, reserve
from fast_channels.functions.pds.store import snapshot_store
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
REQUEST_CONFIG: Final = Config(connect_timeout=0.1, read_timeout=0.1, retries={"total_max_attempts": 1})
"""The reservations' client: on the request path, inside the module's 200 ms."""
RESERVE_BEFORE_S: Final = 0.05
"""No reservation starts later than this into a request: 50 ms, a reservation that reopens its connection (up to about
110 ms) and the load balancer's 20-40 ms fit the module's 200 ms."""


@cache
def dynamodb() -> Any:
    """The reservations' client (:data:`REQUEST_CONFIG`)."""
    return boto3.client("dynamodb", config=REQUEST_CONFIG)


@cache
def budgets() -> Budgets:
    """The lines' budgets, read on the reservations' client: its reads every 5 s keep that connection open."""
    settings().require("pds_table")
    return Budgets(settings().pds_table, lines=lambda: store().current.lines, client=dynamodb())


@cache
def tracker_base() -> str:
    """Our impression tracker on the CDN: ``<content base URL>/ads/b.gif``.

    Raises:
        ValueError: it is not an https URL (the tracker cannot be built).
    """
    settings().require("content_base_url")
    base = f"{settings().content_base_url.rstrip('/')}{TRACKER_PATH}"
    tracker_url(base, "sid.0", "crid")  # raises on a base no tracker can be built on
    return base


@app.post(Endpoint.SELECT.value)
def selection() -> Response[str]:
    """The pod, a 400 for a request that is not the contract's, or a 500: the module then renders house ads."""
    started = time.perf_counter()
    try:
        request = SelectRequest.model_validate_json(app.current_event.decoded_body or "")
    except ValidationError as error:
        logger.warning("select request refused", extra={"fields": refused_fields(error)})
        metrics.add_metric(name="SelectRefused", unit=MetricUnit.Count, value=1)
        return json_response(400, '{"error":"invalid request"}')
    try:
        return _answer(request, started)
    except Exception:
        logger.exception("select failed")
        metrics.add_metric(name="SelectErrors", unit=MetricUnit.Count, value=1)
        return json_response(500, '{"error":"internal"}')


def _reserver(request: SelectRequest, started: float) -> Reserve:
    """The reservation step of ``selection.select`` for this break. It refuses a line no shard can take, every line
    once :data:`RESERVE_BEFORE_S` has passed, and every line of a reservation DynamoDB failed: no line ad is
    answered unreserved."""

    def reserve_lines(wanted: Mapping[Line, tuple[str, ...]]) -> frozenset[str]:
        everything = frozenset(crid for crids in wanted.values() for crid in crids)
        if time.perf_counter() - started > RESERVE_BEFORE_S:
            metrics.add_metric(name="ReservationsTooLate", unit=MetricUnit.Count, value=1)
            return everything
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
            return everything
        return frozenset(refused | set(done.refused))

    return reserve_lines


def _answer(request: SelectRequest, started: float) -> Response[str]:
    logger.set_correlation_id(request.decision_id)
    base = tracker_base()  # before anything is reserved: a bad setting fails the call, not a reservation
    snapshot = store().get()
    rules = pod_context(
        snapshot.book, floor_cpm=snapshot.floor_cpm, scheduled=scheduled_ms(request.dur_ms, snapshot.breaks_ms)
    )
    decision, reserved = select(request, rules, snapshot.book, reserve=_reserver(request, started))
    enr = arm(request.sid, request.evid, settings().crawl_holdout_percent)
    body = select_response(request, decision, tracker_base=base, enr=enr).model_dump_json()
    record = select_record(
        request, decision, latency_ms=round((time.perf_counter() - started) * 1000, 3), reserved=reserved
    )
    logger.info("decision", extra={**record.model_dump(mode="json"), "snapshot": snapshot.version})
    with single_metric(
        name="SelectDecisions", unit=MetricUnit.Count, value=1, default_dimensions=metrics.default_dimensions
    ) as by_reason:
        by_reason.add_dimension(name="reason", value=decision.reason.value)
    metrics.add_metric(name="LineAdsReserved", unit=MetricUnit.Count, value=len(reserved))
    return json_response(200, body)


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)


def warm() -> None:
    """Open the reservations' connection to DynamoDB during init: a first request would otherwise pay its DNS, TCP
    and TLS set-up inside the module's wait (seen live: reservations refused on new execution environments)."""
    if not (lines := store().current.lines):
        return
    settings().require("pds_table")
    try:
        dynamodb().batch_get_item(
            RequestItems={
                settings().pds_table: {"Keys": [item_key(budget_key(lines[0].id, 0))], "ProjectionExpression": "pk"}
            }
        )
    except (BotoCoreError, ClientError) as error:
        logger.warning("the reservations' connection is not open", extra={"error": type(error).__name__})


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # pragma: no cover - read and connect during init, not on a request
    store().load()
    budgets().load()
    warm()
