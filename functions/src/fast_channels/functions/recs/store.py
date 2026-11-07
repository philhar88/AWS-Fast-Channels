"""The recommender's table, ``<prefix>-recs``: one state item per viewer and one item per built rail.

- ``V#<vid>``: when the viewer's account was first seen (the ``auth_time`` of its first session), the key of their
  model rail and when it was built, the last build failure and when, and whether a build is running. The ``vid``
  is the only key and no personal data is stored. The item expires a year after its last write.
- ``REQ#<request hash>``: a built rail (:class:`~fast_channels.core.recs.serve.RailDoc`) with its lineage, keyed by
  the hash of its request, so viewers with identical taste facts share one model call. It expires after
  ``rail_hours``.
"""

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.recs.serve import RailDoc

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

VIEWER: Final = "V#"
REQUEST: Final = "REQ#"
VIEWER_DAYS: Final = 400
"""A viewer's state outlives the consent's 395-day re-prompt, so a returning viewer is not a first session again."""
BUILD_LEASE_SECONDS: Final = 300
"""A build that has not finished after this long is presumed dead, and another may start."""


@dataclass(frozen=True, slots=True)
class ViewerState:
    """``V#<vid>`` as read."""

    first_auth: int | None = None
    rail_key: str | None = None
    built_at: float | None = None
    failure: str | None = None
    failed_at: float | None = None
    building_at: float | None = None

    @classmethod
    def of(cls, item: dict[str, Any] | None) -> ViewerState:
        if not item:
            return cls()

        def number(name: str) -> float | None:
            value = item.get(name)
            return float(value) if value is not None else None

        first = number("first_auth")
        return cls(
            first_auth=int(first) if first is not None else None,
            rail_key=item.get("rail_key"),
            built_at=number("built_at"),
            failure=item.get("failure"),
            failed_at=number("failed_at"),
            building_at=number("building_at"),
        )


def _expiry(now: float) -> int:
    return int(now) + VIEWER_DAYS * 86_400


def seen(table: Table, vid: str, auth_time: int, *, now: float | None = None) -> ViewerState:
    """Record the viewer's first ``auth_time`` if they have none, and return their state."""
    now = time.time() if now is None else now
    response = table.update_item(
        Key={"pk": f"{VIEWER}{vid}"},
        UpdateExpression="SET first_auth = if_not_exists(first_auth, :auth), expires_at = :expires",
        ExpressionAttributeValues={":auth": auth_time, ":expires": _expiry(now)},
        ReturnValues="ALL_NEW",
    )
    return ViewerState.of(response.get("Attributes"))


def viewer(table: Table, vid: str) -> ViewerState:
    """The viewer's state (empty if none)."""
    return ViewerState.of(table.get_item(Key={"pk": f"{VIEWER}{vid}"}).get("Item"))


def claim_build(table: Table, vid: str, *, now: float | None = None) -> bool:
    """Start a build for ``vid`` unless one started in the last :data:`BUILD_LEASE_SECONDS`."""
    now = time.time() if now is None else now
    try:
        table.update_item(
            Key={"pk": f"{VIEWER}{vid}"},
            UpdateExpression="SET building_at = :now, expires_at = :expires",
            ConditionExpression="attribute_not_exists(building_at) OR building_at < :stale",
            ExpressionAttributeValues={
                ":now": int(now),
                ":stale": int(now) - BUILD_LEASE_SECONDS,
                ":expires": _expiry(now),
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def built(table: Table, vid: str, rail_key: str, *, now: float | None = None) -> None:
    """The viewer's model rail is ``REQ#<rail_key>``, built ``now``."""
    now = time.time() if now is None else now
    table.update_item(
        Key={"pk": f"{VIEWER}{vid}"},
        UpdateExpression=(
            "SET rail_key = :key, built_at = :now, expires_at = :expires REMOVE building_at, failure, failed_at"
        ),
        ExpressionAttributeValues={":key": rail_key, ":now": int(now), ":expires": _expiry(now)},
    )


def failed(table: Table, vid: str, reason: str, *, now: float | None = None) -> None:
    """The viewer's last build failed for ``reason`` (a :class:`~fast_channels.core.recs.router.Reason`)."""
    now = time.time() if now is None else now
    table.update_item(
        Key={"pk": f"{VIEWER}{vid}"},
        UpdateExpression="SET failure = :reason, failed_at = :now, expires_at = :expires REMOVE building_at",
        ExpressionAttributeValues={":reason": reason, ":now": int(now), ":expires": _expiry(now)},
    )


def put_rail(table: Table, key: str, doc: RailDoc, *, hours: float, now: float | None = None) -> None:
    """Store a built rail for ``hours``."""
    now = time.time() if now is None else now
    table.put_item(Item={"pk": f"{REQUEST}{key}", "doc": doc.model_dump_json(), "expires_at": int(now + hours * 3600)})


def get_rail(table: Table, key: str, *, now: float | None = None) -> RailDoc | None:
    """A built rail, or ``None`` if it is missing, expired (TTL deletion lags) or unreadable."""
    now = time.time() if now is None else now
    item = table.get_item(Key={"pk": f"{REQUEST}{key}"}).get("Item")
    if not item or float(item.get("expires_at", 0)) <= now:  # type: ignore[arg-type]
        return None
    try:
        return RailDoc.model_validate_json(str(item["doc"]))
    except KeyError, ValidationError:
        return None
