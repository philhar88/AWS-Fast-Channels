"""The snapshot every PDS function serves from, loaded at cold start and refreshed every 30 seconds.

A refresh is one conditional GET of ``snapshot/current.json`` (``If-None-Match`` with the last ETag), so an unchanged
snapshot costs a 304 and no parsing. The S3 client has short timeouts and no retries: a failed refresh keeps the
snapshot already loaded and tries again sooner. A function that never loaded one serves ``EMPTY``, the fail-safe
snapshot (no third-party bidder, no viewer address).
"""

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
CLIENT_CONFIG: Final = Config(
    connect_timeout=0.5, read_timeout=0.5, retries={"total_max_attempts": 1, "mode": "standard"}
)
"""The hot path's S3 client: a refresh never holds a request up for long."""

logger = Logger(child=True)


class SnapshotStore:
    """The current snapshot of one bucket.

    Args:
        bucket: the PDS config bucket.
        client: an S3 client (default: one with :data:`CLIENT_CONFIG`).
        clock: monotonic seconds (tests pass their own).
    """

    def __init__(self, bucket: str, *, client: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._bucket = bucket
        self._client = client
        self._clock = clock
        self._snapshot = EMPTY
        self._etag: str | None = None
        self._next = float("-inf")

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = boto3.client("s3", config=CLIENT_CONFIG)
        return self._client

    def get(self) -> Snapshot:
        """The current snapshot, refreshed first if it is due."""
        now = self._clock()
        if now >= self._next:
            self._refresh(now)
        return self._snapshot

    def _refresh(self, now: float) -> None:
        request: dict[str, str] = {"Bucket": self._bucket, "Key": SNAPSHOT_KEY}
        if self._etag:
            request["IfNoneMatch"] = self._etag
        try:
            response = self.client.get_object(**request)
            snapshot = Snapshot.model_validate_json(response["Body"].read())
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 304:
                self._next = now + REFRESH_SECONDS
                return
            self._failed(now, error)
            return
        except (BotoCoreError, ValidationError, ValueError) as error:
            self._failed(now, error)
            return
        if snapshot.version != self._snapshot.version:
            logger.info("snapshot loaded", extra={"snapshot": snapshot.version})
        self._snapshot, self._etag = snapshot, response.get("ETag")
        self._next = now + REFRESH_SECONDS

    def _failed(self, now: float, error: Exception) -> None:
        logger.warning(
            "snapshot refresh failed",
            extra={"snapshot": self._snapshot.version, "error": type(error).__name__, "detail": str(error)[:300]},
        )
        self._next = now + RETRY_SECONDS
