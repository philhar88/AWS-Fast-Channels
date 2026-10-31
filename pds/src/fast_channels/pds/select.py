"""``POST /select``: the pod the fast-ctv Prebid Server module renders after each auction.

The answer comes from ``fast_channels.core.pds.selection.select``:
- the pod rules run on the current snapshot (the own book's tier-0 deals, the floor, the break of record);
- every own-book line ad in the pod or among its standalones is reserved first (``functions.pds.reservations``, on the
  shard ``Budgets.shard`` picks), and a line whose price cannot be reserved is left out.

Each ad carries our impression tracker on the CDN (``core.pds.pod.tracker_url``), with the break's content-enrichment
arm (``enr``, ``functions.crawl.patch.arm``: 0 with the crawl module off). The module waits
``modules.pds.select_timeout_ms`` for the answer; anything but a 200 with a known schema makes it render the
auction's house ads (``SELECT_FALLBACK``), and before the sidecar has a snapshot it answers 503 at once
(``http.NO_SNAPSHOT``). The reservations use the sidecar's DynamoDB client (100 ms timeouts, one attempt), which the
budget view's reads every 5 s keep connected. Reservations stop after :data:`RESERVE_BEFORE_S`: a line not reserved by
then is left out, never answered unreserved. A reservation that DynamoDB committed after the module gave up stays
until the sweeper releases it. Each answer is logged as a ``pds.select`` decision record, which a subscription
carries into the lake.
"""

import time
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.pbs_config import TRACKER_PATH
from fast_channels.core.pds.models import SelectRequest
from fast_channels.core.pds.pod import scheduled_ms, select_response, tracker_url
from fast_channels.core.pds.records import select_record
from fast_channels.core.pds.selection import Reserve, pod_context, select
from fast_channels.core.pds.snapshot import EMPTY
from fast_channels.functions.crawl.patch import arm
from fast_channels.functions.pds.reservations import Hold, reserve
from fast_channels.functions.settings import settings
from fast_channels.pds import state
from fast_channels.pds.http import INTERNAL, NO_SNAPSHOT, REFUSED, Answer, refused_fields
from fast_channels.pds.telemetry import counters, window

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fast_channels.core.pds.book import Line

logger = Logger(child=True)

store = state.store
"""The sidecar's snapshot store."""
budgets = state.budgets
"""The lines' budgets, on the reservations' client."""
dynamodb = state.dynamodb
"""The reservations' client."""
RESERVE_BEFORE_S: Final = 0.05
"""No reservation starts later than this into a request: 50 ms, and a reservation that has to reopen its connection
(up to about 110 ms) still fits the module's wait."""


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


def answer(body: bytes, started: float) -> Answer:
    """The pod, a 400 for a request that is not the contract's, a 503 without a snapshot, or a 500: the module then
    renders house ads. ``started``: when the request arrived (``time.perf_counter``)."""
    try:
        request = SelectRequest.model_validate_json(body)
    except ValidationError as error:
        logger.warning("select request refused", extra={"fields": refused_fields(error)})
        counters.add("SelectRefused")
        return REFUSED
    try:
        return _answer(request, started)
    except Exception:
        logger.exception("select failed", extra={"correlation_id": request.decision_id})
        counters.add("SelectErrors")
        return INTERNAL


def _reserver(request: SelectRequest, started: float) -> Reserve:
    """The reservation step of ``selection.select`` for this break. It refuses a line no shard can take, every line
    once :data:`RESERVE_BEFORE_S` has passed, and every line of a reservation DynamoDB failed: no line ad is
    answered unreserved."""

    def reserve_lines(wanted: Mapping[Line, tuple[str, ...]]) -> frozenset[str]:
        everything = frozenset(crid for crids in wanted.values() for crid in crids)
        if time.perf_counter() - started > RESERVE_BEFORE_S:
            counters.add("ReservationsTooLate")
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
        reserving = time.perf_counter()
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
            logger.warning(
                "reservation failed",
                extra={
                    "error": type(error).__name__,
                    "detail": str(error)[:300],
                    "correlation_id": request.decision_id,
                },
            )
            counters.add("ReservationErrors")
            return everything
        finally:
            window.add("reserve_ms", (time.perf_counter() - reserving) * 1000)
        for reason in done.refused.values():  # budget, cap, or conflict: concurrent breaks on the same shard
            counters.add("LineAdsRefused", dimension=("reason", reason.value))
        return frozenset(refused | set(done.refused))

    return reserve_lines


def _answer(request: SelectRequest, started: float) -> Answer:
    base = tracker_base()  # before anything is reserved: a bad setting fails the call, not a reservation
    snapshot = store().get()
    if snapshot.version == EMPTY.version:
        counters.add("SelectNoSnapshot")
        return NO_SNAPSHOT
    rules = pod_context(
        snapshot.book, floor_cpm=snapshot.floor_cpm, scheduled=scheduled_ms(request.dur_ms, snapshot.breaks_ms)
    )
    decision, reserved = select(request, rules, snapshot.book, reserve=_reserver(request, started))
    enr = arm(request.sid, request.evid, settings().crawl_holdout_percent)
    body = select_response(request, decision, tracker_base=base, enr=enr).model_dump_json()
    record = select_record(
        request, decision, latency_ms=round((time.perf_counter() - started) * 1000, 3), reserved=reserved
    )
    logger.info(
        "decision",
        extra={**record.model_dump(mode="json"), "snapshot": snapshot.version, "correlation_id": request.decision_id},
    )
    counters.add("SelectDecisions", dimension=("reason", decision.reason.value))
    counters.add("LineAdsReserved", len(reserved))
    return Answer(200, body)
