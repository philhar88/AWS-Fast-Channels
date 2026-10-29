"""The Release Controller's state (table ``<prefix>-release``) and its inputs: bundles from the release bucket and the
origin secret from Secrets Manager.

Items, all keyed by ``pk`` alone, hold their model as JSON in ``doc`` so nothing goes through DynamoDB's number
conversion:

- ``COUNTER``: ``last``, the last release number allocated;
- ``REQUEST#<id>``: the release of one ``Desired State Rendered`` event (its ``correlation_id``, the deployment's
  CloudFormation request): a redelivered event gets the same release, a deployment of an earlier bundle a new one;
- ``RELEASE#rNNNN``: :class:`~fast_channels.core.monetization.release.ReleaseStatus`;
- ``CHANNEL#<channel>``: :class:`~fast_channels.core.monetization.release.ChannelState`, written with a version check;
- ``ORIGIN``: the slot (``a`` or ``b``) of the origin secret the siblings send (rotated by M3-05).
"""

import json
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


class StaleStateError(RuntimeError):
    """A channel's state changed between read and write (another execution); the step is retried."""


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


def origin_slot() -> str:
    """The origin secret the siblings send now (``a`` until the first rotation)."""
    item = _get(ORIGIN)
    slot = str(item["slot"]) if item else ORIGIN_SECRET_SLOTS[0]
    if slot not in ORIGIN_SECRET_SLOTS:
        raise ValueError(f"unknown origin secret slot {slot!r}")
    return slot


def secret(slot: str) -> SecretStr:
    """Origin secret ``slot`` of the deployment (DemandEdge, in us-east-1)."""
    config = settings()
    config.require("prefix", "origin_secret_region")
    client = aws.regional_client("secretsmanager", config.origin_secret_region)
    return SecretStr(client.get_secret_value(SecretId=origin_secret(config.prefix, slot))["SecretString"])
