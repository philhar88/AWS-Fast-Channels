"""What the endpoints serve from: the snapshot, the lines' budgets and the reservations' DynamoDB client, loaded once
before the server listens (:func:`load`) and refreshed in background threads off the request path
(``functions.pds.store``: the snapshot every 30 s, the budgets every 5 s).

A refresh starts when a value is read and due, so values read only by requests go stale, and DynamoDB drops the
reservations' idle connection, between breaks. The process never freezes, as a Lambda execution environment does, so
the server reads them every :data:`REFRESH_TICK_S` as well (:func:`refresh`): the snapshot stays current, and the
budget view's reads every 5 s keep the reservations' connection open between breaks. The own book and ``/select``
share that one view, whose reads are timed into the minute's stats (``budget_read_ms``): a DynamoDB round trip from
the task every 5 s.

Every client is created here, on one thread, before the worker threads use it: boto3 makes clients from a session that
is not thread-safe, and the clients themselves are.
"""

import time
from functools import cache
from typing import Any, Final

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from fast_channels.core.pds.delivery import budget_key
from fast_channels.core.pds.snapshot import EMPTY
from fast_channels.functions.crawl.patch import catalog_contexts, contributor
from fast_channels.functions.pds.budget import Budgets
from fast_channels.functions.pds.reservations import item_key
from fast_channels.functions.pds.store import snapshot_store
from fast_channels.functions.settings import settings
from fast_channels.pds.telemetry import window

WORKERS: Final = 16
"""The server's worker threads, and the reservations' connections: every endpoint runs on one, so neither the pod
rules' CPU nor a reservation's round trip holds the event loop."""
REFRESH_TICK_S: Final = 1.0
"""How often the server starts the refreshes that are due (:func:`refresh`)."""
REQUEST_CONFIG: Final = Config(
    connect_timeout=0.1, read_timeout=0.1, retries={"total_max_attempts": 1}, max_pool_connections=WORKERS
)
"""The reservations' client: on the request path, inside the module's wait for ``/select``."""

logger = Logger(child=True)

store = snapshot_store
"""The sidecar's snapshot store."""


@cache
def dynamodb() -> Any:
    """The reservations' client (:data:`REQUEST_CONFIG`), which the budget view reads on too."""
    return boto3.client("dynamodb", config=REQUEST_CONFIG)


class TimedBudgets(Budgets):
    """The budget view, each read's duration into the minute's stats (``budget_read_ms``)."""

    def _fetch(self) -> Any:
        started = time.perf_counter()
        try:
            return super()._fetch()
        finally:
            if self._lines():
                window.add("budget_read_ms", (time.perf_counter() - started) * 1000)


@cache
def budgets() -> Budgets:
    """The lines' budgets, read on the reservations' client: its reads every 5 s keep that connection open."""
    settings().require("pds_table")
    return TimedBudgets(settings().pds_table, lines=lambda: store().current.lines, client=dynamodb())


def refresh() -> None:
    """Start every refresh that is due, each in a thread of its own (it never waits): the snapshot, the budget view
    and, with the crawl module, the catalog contexts."""
    store().get()
    budgets().get()
    if contributor() is not None:
        catalog_contexts().get()


def has_snapshot() -> bool:
    """Whether the sidecar has loaded a snapshot (it serves none until then: ``http.NO_SNAPSHOT``)."""
    return store().current.version != EMPTY.version


def warm() -> float | None:
    """Open the reservations' connection to DynamoDB before the first break: that break would otherwise pay its DNS,
    TCP and TLS set-up inside the module's wait. Returns how long the first call took (ms), or None."""
    if not (lines := store().current.lines):
        return None
    settings().require("pds_table")
    started = time.perf_counter()
    try:
        dynamodb().batch_get_item(
            RequestItems={
                settings().pds_table: {"Keys": [item_key(budget_key(lines[0].id, 0))], "ProjectionExpression": "pk"}
            }
        )
    except (BotoCoreError, ClientError) as error:
        logger.warning("the reservations' connection is not open", extra={"error": type(error).__name__})
        return None
    return round((time.perf_counter() - started) * 1000, 1)


def load() -> dict[str, Any]:
    """Load everything the endpoints serve from, and open the reservations' connection; returns what was loaded
    and how long it took. A load that fails leaves the empty value, which its refresh replaces once it succeeds."""
    started = time.perf_counter()
    snapshot = store().load()
    reading = time.perf_counter()
    budgets().load()  # the client's first call: DNS, TCP and TLS, then the budget shards
    first_call_ms = round((time.perf_counter() - reading) * 1000, 1) if snapshot.lines else None
    second_call_ms = warm()
    contexts = len(catalog_contexts().load()) if contributor() is not None else None
    return {
        "snapshot": snapshot.version if has_snapshot() else None,
        "lines": len(snapshot.lines),
        "catalog_contexts": contexts,
        "dynamodb_first_call_ms": first_call_ms,
        "dynamodb_second_call_ms": second_call_ms,
        "load_ms": round((time.perf_counter() - started) * 1000, 1),
    }
