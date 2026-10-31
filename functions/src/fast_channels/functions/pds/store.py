"""What the PDS serves from, loaded once and refreshed off the request path: the snapshot (every 30 s) and, for the
own book and ``/select``, the lines' budgets (:mod:`fast_channels.functions.pds.budget`). The PDS sidecar
(``fast_channels.pds.state``) and the publisher's functions use them.

``load()`` reads the value once (a function's init, the sidecar's startup). Afterwards ``get()`` never waits: when a
refresh is due it starts one in a background thread and serves what it has; the sidecar also calls ``get()`` every
second. A failed refresh keeps the value already loaded and is tried again sooner.

The snapshot is ``snapshot/current.json``, read with a conditional GET (``If-None-Match`` with the last ETag, so an
unchanged snapshot costs a 304 and no parsing). Until one loads, the store holds ``EMPTY``, the fail-safe snapshot
(no third-party bidder, no viewer address, no own book), and the sidecar answers ``/policy`` and ``/select`` 503.
"""

import threading
import time
from collections.abc import Callable
from functools import cache
from typing import Any, Final

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.pds.snapshot import EMPTY, REFRESH_SECONDS, SNAPSHOT_KEY, Snapshot
from fast_channels.functions.settings import settings

RETRY_SECONDS: Final = 5.0
"""After a failed refresh, how soon the next one is tried."""
CLIENT_CONFIG: Final = Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 2, "mode": "standard"})
"""Client of the refreshes (off the request path: a slow one only delays the next value)."""

logger = Logger(child=True)

type Spawn = Callable[[Callable[[], None]], None]


def spawn_thread(target: Callable[[], None]) -> None:
    """Run ``target`` in a daemon thread."""
    threading.Thread(target=target, name="pds-refresh", daemon=True).start()


class Refreshed[T]:
    """A value refreshed every ``every`` seconds in the background (module docstring).

    Args:
        initial: what :meth:`get` serves until a refresh succeeds.
        every: seconds between refreshes.
        clock: monotonic seconds (tests pass their own).
        spawn: runs a refresh off the request path (default: a daemon thread; tests run it inline).
    """

    name = "value"

    def __init__(
        self, initial: T, *, every: float, clock: Callable[[], float] = time.monotonic, spawn: Spawn = spawn_thread
    ) -> None:
        self._value = initial
        self._every = every
        self._clock = clock
        self._spawn = spawn
        self._next = float("-inf")
        self._refreshing = threading.Lock()

    def load(self) -> T:
        """Read the value now (the function's init)."""
        self._refresh()
        return self._value

    @property
    def current(self) -> T:
        """The value it has, without starting a refresh."""
        return self._value

    def get(self) -> T:
        """The current value; starts a background refresh first if one is due and none is running."""
        if self._clock() >= self._next and self._refreshing.acquire(blocking=False):
            self._spawn(self._refresh_and_release)
        return self._value

    def _fetch(self) -> T | None:
        """The new value, or None when it is unchanged. Raises on failure."""
        raise NotImplementedError

    def _refresh_and_release(self) -> None:
        try:
            self._refresh()
        finally:
            self._refreshing.release()

    def _refresh(self) -> None:
        started = self._clock()
        try:
            value = self._fetch()
        except (BotoCoreError, ClientError, ValidationError, ValueError) as error:
            logger.warning(
                "refresh failed",
                extra={
                    "refreshed": self.name,
                    "error": type(error).__name__,
                    "detail": str(error)[:300],
                    **self._describe(self._value),
                },
            )
            self._next = started + RETRY_SECONDS
            return
        if value is not None:
            self._value = value
        self._next = started + self._every

    def _describe(self, value: T) -> dict[str, Any]:
        """What a log line says about ``value``."""
        return {}


class SnapshotStore(Refreshed[Snapshot]):
    """The current snapshot of one bucket.

    Args:
        bucket: the PDS config bucket.
        client: an S3 client (default: one with :data:`CLIENT_CONFIG`).
        clock: monotonic seconds (tests pass their own).
        spawn: runs a refresh off the request path (default: a daemon thread; tests run it inline).
    """

    name = "snapshot"

    def __init__(
        self,
        bucket: str,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Spawn = spawn_thread,
    ) -> None:
        super().__init__(EMPTY, every=REFRESH_SECONDS, clock=clock, spawn=spawn)
        self._bucket = bucket
        self._client = client
        self._etag: str | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", config=CLIENT_CONFIG)
        return self._client

    def _fetch(self) -> Snapshot | None:
        request: dict[str, str] = {"Bucket": self._bucket, "Key": SNAPSHOT_KEY}
        if self._etag:
            request["IfNoneMatch"] = self._etag
        try:
            response = self.client.get_object(**request)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 304:
                return None
            raise
        snapshot = Snapshot.model_validate_json(response["Body"].read())
        if snapshot.version != self._value.version:
            logger.info("snapshot loaded", extra={"snapshot": snapshot.version})
        self._etag = response.get("ETag")
        return snapshot

    def _describe(self, value: Snapshot) -> dict[str, Any]:
        return {"snapshot": value.version}


@cache
def snapshot_store() -> SnapshotStore:
    """The function's snapshot store (one per execution environment), on ``PDS_CONFIG_BUCKET``."""
    settings().require("pds_config_bucket")
    return SnapshotStore(settings().pds_config_bucket)
