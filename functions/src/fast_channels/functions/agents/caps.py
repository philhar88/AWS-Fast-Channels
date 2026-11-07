"""The envelope's caps (``core.agents.envelope.Cap``): an approved action takes one unit of each cap, or of none.

Each cap is one item of the agents' table, ``pk`` its key and ``sk`` its window's start, with the count ``n`` and a
TTL a day after the window. :func:`take` adds one to every cap of an action in a single ``TransactWriteItems``, each
update conditional on its count being under its limit, so the counts never exceed their limits however many runners
approve at once: of two concurrent restarts under a cap of one, exactly one is approved. A transaction that lost a
race with another (``TransactionConflict``) is tried again.
"""

import time
from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Final

from botocore.exceptions import ClientError

from fast_channels.core.agents.envelope import WINDOW, Cap

ATTEMPTS: Final = 5
"""Transactions per take: the first, and retries after a conflict with a concurrent take."""
RETRY_PAUSE_S: Final = 0.05
KEEP: Final = timedelta(days=1)
"""How long after its window a counter item stays (``ttl``): it is never read again."""
CANCELLED: Final = "TransactionCanceledException"


def take(client: Any, table: str, caps: Sequence[Cap]) -> Cap | None:
    """Take one unit of each of ``caps``; returns the cap that was full (nothing is taken then), or None.

    Raises:
        ClientError: any other DynamoDB error, or a conflict that outlasted :data:`ATTEMPTS`.
    """
    if full := next((cap for cap in caps if cap.limit <= 0), None):
        return full
    actions = [
        {
            "Update": {
                "TableName": table,
                "Key": {"pk": {"S": cap.key}, "sk": {"S": cap.window.isoformat()}},
                "UpdateExpression": "ADD n :one SET #ttl = :ttl",
                "ConditionExpression": "attribute_not_exists(n) OR n < :limit",
                "ExpressionAttributeNames": {"#ttl": "ttl"},
                "ExpressionAttributeValues": {
                    ":one": {"N": "1"},
                    ":limit": {"N": str(cap.limit)},
                    ":ttl": {"N": str(int((cap.window + WINDOW + KEEP).timestamp()))},
                },
            }
        }
        for cap in caps
    ]
    for attempt in range(1, ATTEMPTS + 1):
        try:
            client.transact_write_items(TransactItems=actions)
        except ClientError as error:
            reasons = _reasons(error)
            if reasons is None:
                raise
            if failed := [cap for cap, code in zip(caps, reasons, strict=False) if code == "ConditionalCheckFailed"]:
                return failed[0]
            if attempt == ATTEMPTS:
                raise
            time.sleep(RETRY_PAUSE_S * attempt)
            continue
        return None
    return None  # unreachable: the last attempt returns or raises


def _reasons(error: ClientError) -> list[str] | None:
    """Each action's cancellation code (``None`` where it passed), or None when ``error`` is not a cancellation."""
    if error.response.get("Error", {}).get("Code") != CANCELLED:
        return None
    reasons = error.response.get("CancellationReasons") or []
    return [str(reason.get("Code") or "None") for reason in reasons]
