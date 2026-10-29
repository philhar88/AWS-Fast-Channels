"""The snapshot every PDS function serves from, loaded during init and refreshed every 30 seconds off the request path.

``load()`` reads ``snapshot/current.json`` once, during the function's init. Afterwards ``get()`` never waits on S3:
when a refresh is due it starts one in a background thread (a conditional GET, ``If-None-Match`` with the last ETag,
so an unchanged snapshot costs a 304 and no parsing) and serves the snapshot it has. Lambda freezes the thread between
invocations; it carries on during the next one. A failed refresh keeps the snapshot already loaded and is tried again
sooner. A function that never loaded one serves ``EMPTY``, the fail-safe snapshot (no third-party bidder, no viewer
address).
"""

import threading
import time
from collections.abc import Callable
from typing import Any, Final

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.pds.snapshot import EMPTY, REFRESH_SECONDS, SNAPSHOT_KEY, Snapshot

RETRY_SECONDS: Final = 5.0
"""After a failed refresh, how soon the next one is tried."""
CLIENT_CONFIG: Final = Config(connect_timeout=1, read_timeout=1, retries={"total_max_attempts": 2, "mode": "standard"})
"""S3 client of the refreshes (off the request path: a slow one only delays the next snapshot)."""

logger = Logger(child=True)


def _thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="snapshot-refresh", daemon=True).start()


class SnapshotStore:
    """The current snapshot of one bucket.

    Args:
        bucket: the PDS config bucket.
        client: an S3 client (default: one with :data:`CLIENT_CONFIG`).
        clock: monotonic seconds (tests pass their own).
        spawn: runs a refresh off the request path (default: a daemon thread; tests run it inline).
    """

    def __init__(
        self,
        bucket: str,
        *,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _thread,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._clock = clock
        self._spawn = spawn
        self._snapshot = EMPTY
        self._etag: str | None = None
        self._next = float("-inf")
        self._refreshing = threading.Lock()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", config=CLIENT_CONFIG)
        return self._client

    def load(self) -> Snapshot:
        """Read the snapshot now (the function's init)."""
        self._refresh()
        return self._snapshot

    def get(self) -> Snapshot:
        """The current snapshot; starts a background refresh first if one is due and none is running."""
        if self._clock() >= self._next and self._refreshing.acquire(blocking=False):
            self._spawn(self._refresh_and_release)
        return self._snapshot

    def _refresh_and_release(self) -> None:
        try:
            self._refresh()
        finally:
            self._refreshing.release()

    def _refresh(self) -> None:
        request: dict[str, str] = {"Bucket": self._bucket, "Key": SNAPSHOT_KEY}
        if self._etag:
            request["IfNoneMatch"] = self._etag
        started = self._clock()
        try:
            response = self.client.get_object(**request)
            snapshot = Snapshot.model_validate_json(response["Body"].read())
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 304:
                self._next = started + REFRESH_SECONDS
                return
            self._failed(started, error)
            return
        except (BotoCoreError, ValidationError, ValueError) as error:
            self._failed(started, error)
            return
        if snapshot.version != self._snapshot.version:
            logger.info("snapshot loaded", extra={"snapshot": snapshot.version})
        self._snapshot, self._etag = snapshot, response.get("ETag")
        self._next = started + REFRESH_SECONDS

    def _failed(self, started: float, error: Exception) -> None:
        logger.warning(
            "snapshot refresh failed",
            extra={"snapshot": self._snapshot.version, "error": type(error).__name__, "detail": str(error)[:300]},
        )
        self._next = started + RETRY_SECONDS
