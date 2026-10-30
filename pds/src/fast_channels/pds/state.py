"""What the endpoints serve from: the snapshot, the lines' budgets and the reservations' DynamoDB client, loaded once
before the server listens (:func:`load`) and refreshed in background threads off the request path
(``functions.pds.store``: the snapshot every 30 s, the budgets every 5 s).

The process never freezes between requests, as a Lambda execution environment does, so the refreshes run on time and
the budget view's reads every 5 s keep the reservations' connection to DynamoDB open while breaks follow each other.
The own book and ``/select`` share that one view.

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

WORKERS: Final = 16
"""The server's worker threads, and the reservations' connections: every endpoint runs on one, so neither the pod
rules' CPU nor a reservation's round trip holds the event loop."""
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


@cache
def budgets() -> Budgets:
    """The lines' budgets, read on the reservations' client: its reads every 5 s keep that connection open."""
    settings().require("pds_table")
    return Budgets(settings().pds_table, lines=lambda: store().current.lines, client=dynamodb())


def has_snapshot() -> bool:
    """Whether the sidecar has loaded a snapshot (it serves none until then: ``http.NO_SNAPSHOT``)."""
    return store().current.version != EMPTY.version


def warm() -> None:
    """Open the reservations' connection to DynamoDB before the first break: that break would otherwise pay its DNS,
    TCP and TLS set-up inside the module's wait."""
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


def load() -> dict[str, Any]:
    """Load everything the endpoints serve from, and open the reservations' connection; returns what was loaded
    and how long it took. A load that fails leaves the empty value, which its refresh replaces once it succeeds."""
    started = time.perf_counter()
    snapshot = store().load()
    budgets().load()
    warm()
    contexts = len(catalog_contexts().load()) if contributor() is not None else None
    return {
        "snapshot": snapshot.version if has_snapshot() else None,
        "lines": len(snapshot.lines),
        "catalog_contexts": contexts,
        "load_ms": round((time.perf_counter() - started) * 1000, 1),
    }
