"""Append to the ledger (DESIGN-v2.2 §18.5): what every producer (runbooks, the Release Controller, the PDS) calls.

    from fast_channels.functions.ops.ledger import append, entry
    append(entry("runbook/r13", LedgerKind.RUNBOOK, principal=..., action="runbook.swap", subject="fastm-demo"))

An entry is put only if its key is new, so a retried call never overwrites, and an entry is never changed afterwards:
the ledger writer links it into its chain from the table's stream and archives it under Object Lock.
"""

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from fast_channels.core.ledger import Json, LedgerEntry, LedgerKind, entry_item, new_record_id
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Mapping


class DuplicateEntry(RuntimeError):
    """An entry with this chain and record id is already in the ledger."""


def entry(
    chain_id: str,
    kind: LedgerKind,
    *,
    principal: str,
    action: str,
    subject: str,
    correlation_id: str | None = None,
    detail: Mapping[str, Json] | None = None,
    at: datetime | None = None,
) -> LedgerEntry:
    """A new entry, timestamped now (or ``at``) with a fresh ULID."""
    when = at or datetime.now(UTC)
    return LedgerEntry(
        chain_id=chain_id,
        record_id=new_record_id(when, os.urandom(10)),
        kind=kind,
        at=when,
        principal=principal,
        action=action,
        subject=subject,
        correlation_id=correlation_id,
        detail=dict(detail or {}),
    )


def append(item: LedgerEntry, *, table: str | None = None) -> LedgerEntry:
    """Put ``item`` into the ledger table (``LEDGER_TABLE`` unless ``table`` is given), only if it is new.

    Raises:
        DuplicateEntry: the entry's key exists (a retry of a call that succeeded; nothing is changed).
    """
    if table is None:
        settings().require("ledger_table")
        table = settings().ledger_table
    try:
        aws.table(table).put_item(Item=entry_item(item), ConditionExpression="attribute_not_exists(pk)")
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise DuplicateEntry(f"{item.chain_id}/{item.record_id} is already in the ledger") from None
        raise
    return item
