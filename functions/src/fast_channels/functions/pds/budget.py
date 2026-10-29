"""What is left of each own-book line's budget, as the own book reads it: every few seconds, off the request path.

The view reads the lines' budget shards (``BUDGET#<line>`` / ``S#<n>``) with ``BatchGetItem``. A line is
*exhausted* when no shard has its price (``cpm / 1000``) left, or when its shards cannot be read: without them no
reservation could succeed, so the own book does not bid it. The reservation that ``/select`` makes is what enforces
the budget; this view only stops the own book bidding a line that could not be reserved.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import boto3

from fast_channels.core.pds.delivery import BUDGET_SHARDS, budget_key
from fast_channels.functions.pds.store import CLIENT_CONFIG, Refreshed, Spawn, spawn_thread

if TYPE_CHECKING:
    from fast_channels.core.pds.book import Book, Line

REFRESH_SECONDS: Final = 5.0
"""How often the view reads the shards again."""
BATCH: Final = 100
"""``BatchGetItem``'s most keys per call."""
ATTEMPTS: Final = 3
"""Calls per batch while DynamoDB leaves keys unprocessed."""

type Remaining = Mapping[str, tuple[Decimal, ...]]
"""Line id -> what each of its shards has left, in shard order (shards not read are missing)."""


def _key(key: Mapping[str, str]) -> dict[str, dict[str, str]]:
    return {name: {"S": value} for name, value in key.items()}


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
        unread = Decimal(-1)
        return frozenset(
            line.id for line in book.lines if max(remaining.get(line.id, ()), default=unread) < line.insertion
        )

    def _fetch(self) -> Remaining:
        keys = [_key(budget_key(line.id, n)) for line in self._lines() for n in range(BUDGET_SHARDS)]
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
        return {line: tuple(shards[n] for n in sorted(shards)) for line, shards in found.items()}
