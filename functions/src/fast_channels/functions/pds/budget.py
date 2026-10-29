"""What is left of each own-book line's budget, as the own book reads it: every few seconds, off the request path.

The view reads the lines' budget shards (``BUDGET#<line>`` / ``S#<n>``) with ``BatchGetItem``. A line is
*exhausted* when no shard has its price (``cpm / 1000``) left, or when its shards cannot be read: without them no
reservation could succeed, so the own book does not bid it. ``/select`` reserves from the shard :meth:`Budgets.shard`
picks: the break's home shard if it has the amount, else another that has. The reservation is what enforces the
budget; this view only keeps the own book from bidding a line that could not be reserved.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import boto3

from fast_channels.core.pds.delivery import BUDGET_SHARDS, budget_key, home_shard
from fast_channels.functions.pds.reservations import item_key
from fast_channels.functions.pds.store import CLIENT_CONFIG, Refreshed, Spawn, spawn_thread

if TYPE_CHECKING:
    from fast_channels.core.pds.book import Book, Line

REFRESH_SECONDS: Final = 5.0
"""How often the view reads the shards again."""
BATCH: Final = 100
"""``BatchGetItem``'s most keys per call."""
ATTEMPTS: Final = 3
"""Calls per batch while DynamoDB leaves keys unprocessed."""

type Remaining = Mapping[str, Mapping[int, Decimal]]
"""Line id -> shard -> what it has left (shards not read are missing)."""


class Budgets(Refreshed[Remaining]):
    """The lines' remaining budgets.

    Args:
        table: the PDS table.
        lines: the lines to read (the current snapshot's).
        client: a DynamoDB client (default: one with short timeouts).
        clock: monotonic seconds (tests pass their own).
        spawn: runs a refresh off the request path (default: a daemon thread; tests run it inline).
    """

    name = "budget"

    def __init__(
        self,
        table: str,
        *,
        lines: Callable[[], Sequence[Line]],
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Spawn = spawn_thread,
    ) -> None:
        super().__init__({}, every=REFRESH_SECONDS, clock=clock, spawn=spawn)
        self._table = table
        self._lines = lines
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("dynamodb", config=CLIENT_CONFIG)
        return self._client

    def exhausted(self, book: Book) -> frozenset[str]:
        """The lines of ``book`` that no shard can reserve one impression of."""
        remaining = self.get()
        return frozenset(
            line.id
            for line in book.lines
            if max(remaining.get(line.id, {}).values(), default=Decimal(-1)) < line.insertion
        )

    def shard(self, line: Line, decision: str, amount: Decimal) -> int | None:
        """The shard to reserve ``amount`` of ``line`` from in break ``decision``: its home shard if that has the
        amount left, else the first that has; None when none has (or none was read)."""
        shards = self.get().get(line.id, {})
        home = home_shard(decision)
        candidates = [home, *(n for n in sorted(shards) if n != home)]
        return next((n for n in candidates if shards.get(n, Decimal(-1)) >= amount), None)

    def _fetch(self) -> Remaining:
        keys = [item_key(budget_key(line.id, n)) for line in self._lines() for n in range(BUDGET_SHARDS)]
        found: dict[str, dict[int, Decimal]] = {}
        for start in range(0, len(keys), BATCH):
            request: dict[str, Any] = {
                self._table: {"Keys": keys[start : start + BATCH], "ProjectionExpression": "pk, sk, remaining"}
            }
            for _ in range(ATTEMPTS):
                response = self.client.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self._table, []):
                    if "remaining" in item:
                        line = item["pk"]["S"].removeprefix("BUDGET#")
                        found.setdefault(line, {})[int(item["sk"]["S"].removeprefix("S#"))] = Decimal(
                            item["remaining"]["N"]
                        )
                if not (request := response.get("UnprocessedKeys") or {}):
                    break
        return found
