"""The Release Controller's state (table ``<prefix>-release``) and its inputs: bundles from the release bucket and the
origin secret from Secrets Manager.

Items, all keyed by ``pk`` alone, hold their model as JSON in ``doc`` so nothing goes through DynamoDB's number
conversion:

- ``COUNTER``: ``last``, the last release number allocated;
- ``REQUEST#<id>``: the release of one ``Desired State Rendered`` event (its ``correlation_id``, the deployment's
  CloudFormation request): a redelivered event gets the same release, a deployment of an earlier bundle a new one;
- ``RELEASE#rNNNN``: :class:`~fast_channels.core.monetization.release.ReleaseStatus`;
- ``CHANNEL#<channel>``: :class:`~fast_channels.core.monetization.release.ChannelState`, written with a version check;
- ``ORIGIN``: the slot (``a`` or ``b``) of the origin secret the siblings send (rotated by M3-05);
- ``LOCK``: the release applying now (or :data:`ROTATION`, an origin-secret rotation), until ``expires`` (epoch
  seconds): releases and rotations run one at a time;
- ``POINTER``: the release pointer's last deployed content (JSON in ``doc``).
"""

import json
import time
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError
from pydantic import SecretStr

from fast_channels.core.demand import ORIGIN_SECRET_SLOTS, origin_secret
from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.release import ChannelState, ReleaseStatus
from fast_channels.core.monetization.validators import Violation, load_bundle
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

    from fast_channels.core.monetization.bundle import Bundle

COUNTER: Final = "COUNTER"
ORIGIN: Final = "ORIGIN"
LOCK: Final = "LOCK"
POINTER: Final = "POINTER"
ROTATION: Final = 0
"""The lock's holder while the origin secret rotates (releases are numbered from 1)."""
LOCK_SECONDS: Final = 5400
"""How long a lock outlives an execution that died holding it, unless ``RELEASE_LOCK_SECONDS`` (the state machine's
timeout) says otherwise."""


class StaleStateError(RuntimeError):
    """A channel's state changed between read and write (another execution); the step is retried."""


class ReleaseBusyError(RuntimeError):
    """Another release holds the lock; the step is retried until it gives the lock back (or its lock expires)."""


def _table() -> Table:
    settings().require("release_table")
    return aws.table(settings().release_table)


def _get(pk: str) -> dict[str, Any] | None:
    item: dict[str, Any] | None = _table().get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
    return item


def load(key: str) -> tuple[Bundle | None, list[Violation], str]:
    """The bundle document at ``key`` in the release bucket: the bundle (None if its shape is invalid), every
    violation, and the document's text."""
    settings().require("release_bucket")
    text = aws.client("s3").get_object(Bucket=settings().release_bucket, Key=key)["Body"].read().decode()
    bundle, violations = load_bundle(json.loads(text))
    return bundle, violations, text


@cache
def bundle(key: str) -> Bundle:
    """The bundle at ``key`` (validated when it was released; cached for the execution environment's life).

    Raises:
        ValueError: the document no longer parses.
    """
    found, _, _ = load(key)
    if found is None:
        raise ValueError(f"{key} is not a bundle document")
    return found


def release_for(request: str) -> int | None:
    """The release allocated to the event ``request``, if any."""
    item = _get(f"REQUEST#{request}")
    return int(item["release"]) if item else None


def allocate(request: str) -> int:
    """A new release number for the event ``request``, or the one it already has (an event can be delivered
    twice)."""
    if (existing := release_for(request)) is not None:
        return existing
    table = _table()
    release = int(
        table.update_item(  # type: ignore[arg-type]
            Key={"pk": COUNTER},
            UpdateExpression="ADD #last :one",
            ExpressionAttributeNames={"#last": "last"},
            ExpressionAttributeValues={":one": 1},
            ReturnValues="UPDATED_NEW",
        )["Attributes"]["last"]
    )
    try:
        table.put_item(
            Item={"pk": f"REQUEST#{request}", "release": release}, ConditionExpression="attribute_not_exists(pk)"
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return release_for(request) or release  # a concurrent execution allocated it first
    return release


def status(release: int) -> ReleaseStatus | None:
    item = _get(f"RELEASE#{release_tag(release)}")
    return ReleaseStatus.model_validate_json(item["doc"]) if item else None


def put_status(record: ReleaseStatus) -> None:
    _table().put_item(Item={"pk": f"RELEASE#{release_tag(record.release)}", "doc": record.model_dump_json()})


def channel(name: str) -> tuple[ChannelState | None, int]:
    """A channel's state and its version (0 before the channel's first release)."""
    item = _get(f"CHANNEL#{name}")
    if item is None:
        return None, 0
    return ChannelState.model_validate_json(item["doc"]), int(item["version"])


def channels(names: list[str]) -> dict[str, ChannelState]:
    """The states of ``names`` that have one."""
    found = {name: channel(name)[0] for name in names}
    return {name: state for name, state in found.items() if state is not None}


def put_channel(state: ChannelState, *, version: int) -> None:
    """Write ``state`` if the stored one is still at ``version``.

    Raises:
        StaleStateError: another execution wrote it first.
    """
    try:
        _table().put_item(
            Item={"pk": f"CHANNEL#{state.channel}", "doc": state.model_dump_json(), "version": version + 1},
            ConditionExpression="attribute_not_exists(pk) OR #version = :version",
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues={":version": version},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise StaleStateError(f"{state.channel} changed while it was being released") from None
        raise


def lock(release: int) -> None:
    """Hold the controller's lock for ``release`` (again, when it already does).

    Raises:
        ReleaseBusyError: another release holds it and its lock has not expired.
    """
    now = int(time.time())
    try:
        _table().put_item(
            Item={"pk": LOCK, "release": release, "expires": now + (settings().release_lock_seconds or LOCK_SECONDS)},
            ConditionExpression="attribute_not_exists(pk) OR #release = :release OR #expires < :now",
            ExpressionAttributeNames={"#release": "release", "#expires": "expires"},
            ExpressionAttributeValues={":release": release, ":now": now},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            waiting = release_tag(release) if release != ROTATION else "the rotation"
            raise ReleaseBusyError(f"another release or rotation holds the lock; {waiting} waits") from None
        raise


def holder() -> int | None:
    """The release that holds the lock now (an expired lock holds nothing)."""
    item = _get(LOCK)
    if item is None or int(item["expires"]) < int(time.time()):
        return None
    return int(item["release"])


def unlock(release: int) -> None:
    """Give the lock back if ``release`` holds it."""
    try:
        _table().delete_item(
            Key={"pk": LOCK},
            ConditionExpression="#release = :release",
            ExpressionAttributeNames={"#release": "release"},
            ExpressionAttributeValues={":release": release},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def origin_slot() -> str:
    """The origin secret the siblings send now (``a`` until the first rotation)."""
    item = _get(ORIGIN)
    slot = str(item["slot"]) if item else ORIGIN_SECRET_SLOTS[0]
    if slot not in ORIGIN_SECRET_SLOTS:
        raise ValueError(f"unknown origin secret slot {slot!r}")
    return slot


def put_origin_slot(slot: str) -> None:
    """Record the origin secret the siblings send from now on (the rotation's switch)."""
    if slot not in ORIGIN_SECRET_SLOTS:
        raise ValueError(f"unknown origin secret slot {slot!r}")
    _table().put_item(Item={"pk": ORIGIN, "slot": slot})


def secret(slot: str) -> SecretStr:
    """Origin secret ``slot`` of the deployment (DemandEdge, in us-east-1)."""
    config = settings()
    config.require("prefix", "origin_secret_region")
    client = aws.regional_client("secretsmanager", config.origin_secret_region)
    return SecretStr(client.get_secret_value(SecretId=origin_secret(config.prefix, slot))["SecretString"])


def pointer() -> dict[str, str]:
    """The release pointer's last deployed content (empty before the first)."""
    item = _get(POINTER)
    return dict(json.loads(item["doc"])) if item else {}


def put_pointer(content: dict[str, str]) -> None:
    _table().put_item(Item={"pk": POINTER, "doc": json.dumps(content, sort_keys=True)})
