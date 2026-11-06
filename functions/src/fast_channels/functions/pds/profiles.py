"""Viewer profiles in the PDS table (``core.pds.viewers``): the seed script writes them, the sidecar reads them.

- :func:`put_profile` writes the profile of a demo persona's viewer id, with the tier the persona list gives it. The
  seed script calls it once per signed-in persona, after it created the user and derived the viewer id.
- :func:`read_tier` reads a viewer's tier, which the sidecar does off the request path (``fast_channels.pds.viewers``).
"""

from datetime import UTC, datetime
from typing import Any

from fast_channels.core.pds.viewers import profile_item, profile_key, tier_of
from fast_channels.functions.pds.reservations import item_key


def put_profile(dynamodb: Any, table: str, vid: str, persona: str, *, now: datetime | None = None) -> None:
    """Write the profile of the viewer ``vid``, who is the persona ``persona``, to ``table`` (``<prefix>-pds``).

    Args:
        dynamodb: a boto3 DynamoDB client.
        table: the PDS table (``fast_channels.core.pds.names.table_name``).
        vid: the viewer id (32 lowercase hex characters).
        persona: a persona id with a tier (``fast_channels.core.personas``): not a kids profile or the guest.

    Raises:
        ValueError: an invalid ``vid``, an unknown persona, or one without a tier.
    """
    item = profile_item(vid, persona, now=now or datetime.now(UTC))
    dynamodb.put_item(TableName=table, Item={name: {"S": value} for name, value in item.items()})


def read_tier(dynamodb: Any, table: str, vid: str) -> str | None:
    """The tier in ``vid``'s profile; ``None`` without a profile (an eventually consistent read: a profile changes
    at most daily, and a viewer without one gets the control plan)."""
    answer = dynamodb.get_item(TableName=table, Key=item_key(profile_key(vid)), ProjectionExpression="tier")
    return tier_of(answer.get("Item"))
