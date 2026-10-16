"""The per-channel schedule-write lease, shared by every tool that changes a channel's programs.

Channel Assembly rejects concurrent schedule edits with ConflictException (channel-assembly.md §5.2), so one writer
per channel holds the conditional ``LEASE`` item: publishing a plan, removing programs, and starting a stopped
channel (which removes the programs it missed).
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from fast_channels.functions.channel_ops import error_code
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.registry import ToolError

LEASE_TTL_S: Final = 360
"""Longer than the tool functions' 5-minute timeout, so a crashed writer frees the channel by itself."""
RETRY_HINT: Final = "Wait a minute, then plan again from the fresh schedule."


@contextmanager
def lease(channel: str, owner: str, *, hint: str = RETRY_HINT) -> Iterator[None]:
    """Exclusive schedule-write lease on ``channel`` for the duration of the block; a held lease raises a retryable
    ToolError CONFLICT with ``hint``."""
    key = store.lease_key(channel)
    now_s = int(time.time())
    try:
        store.put_item(
            {**key, "owner": owner, "expires_at": now_s + LEASE_TTL_S},
            condition=Attr("pk").not_exists() | Attr("expires_at").lt(now_s),
        )
    except ClientError as error:
        if error_code(error) != "ConditionalCheckFailedException":
            raise
        raise ToolError(
            "CONFLICT", f"another schedule change on {channel} is in progress", retryable=True, hint=hint
        ) from None
    try:
        yield
    finally:
        try:
            store.table().delete_item(Key=key, ConditionExpression=Attr("owner").eq(owner))
        except ClientError as error:
            if error_code(error) != "ConditionalCheckFailedException":
                raise
