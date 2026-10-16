"""The ``MediaLibrary`` single-table schema (CONTRACTS §D): keys, index attributes, statuses and attribute codecs.

The one definition every component reads and writes through: the ingest steps, the agent tools
(:mod:`fast_channels.functions.tools.library_store` adds typed records and queries on top), the channel custom
resources and the as-run reconciler. Pure: no I/O.

=================  ======================  ===============================  =========================================
Entity             pk                      sk                               gsi1pk / gsi1sk
=================  ======================  ===============================  =========================================
Asset              ``ASSET#<asset_id>``    ``LADDER#<ladder_version>``      ``KIND#<kind>#<ladder_version>`` /
                                                                            ``<status>#<updated_at>``
Ladder             ``LADDER#<ladder_id>``  ``V#<ladder_version>``           ``LADDERS`` / ``<status>#<ladder_version>``
Ladder proposal    ``PROPOSAL#<id>``       ``META`` (TTL)                   -
Channel            ``CHANNEL#<name>``      ``META``                         ``CHANNELS`` / ``<owner>#<name>``
Program (EPG)      ``CHANNEL#<name>``      ``PROGRAM#<start_ms:013d>`` TTL  ``PROGRAM#<program name>`` / ``<channel>``
Plan               ``CHANNEL#<name>``      ``PLAN#<plan_id>`` (TTL)         -
Lease              ``CHANNEL#<name>``      ``LEASE`` (TTL)                  -
=================  ======================  ===============================  =========================================

Attributes are snake_case, timestamps ISO-8601 UTC strings and :data:`TTL_ATTRIBUTE` epoch seconds.
"""

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from fast_channels.core.tags import Owner

__all__ = [
    "ASSET_PREFIX",
    "CHANNEL_PREFIX",
    "GSI1",
    "GSI1_KEYS",
    "MAX_EPOCH_MS",
    "PROGRAM_PREFIX",
    "PROGRAM_RETENTION_S",
    "TABLE_KEYS",
    "TTL_ATTRIBUTE",
    "AssetStatus",
    "ItemKey",
    "LadderStatus",
    "Owner",
    "ProgramStatus",
    "asset_index",
    "asset_key",
    "channel_index",
    "channel_key",
    "expires_at",
    "from_dynamo",
    "iso",
    "ladder_index",
    "ladder_key",
    "lease_key",
    "now_iso",
    "plan_key",
    "program_expires_at",
    "program_index",
    "program_key",
    "program_sk",
    "proposal_key",
    "to_dynamo",
    "update_request",
]

GSI1: Final = "GSI1"
TTL_ATTRIBUTE: Final = "expires_at"
TABLE_KEYS: Final = ("pk", "sk")
GSI1_KEYS: Final = ("pk", "sk", "gsi1pk", "gsi1sk")
"""ExclusiveStartKey attributes of a base-table read and of a GSI1 query."""
ASSET_PREFIX: Final = "ASSET#"
CHANNEL_PREFIX: Final = "CHANNEL#"
PROGRAM_PREFIX: Final = "PROGRAM#"
MAX_EPOCH_MS: Final = 10**13 - 1
"""Program sort keys are zero-padded to 13 digits."""
PROGRAM_RETENTION_S: Final = 30 * 86_400
"""How long a program's EPG item outlives the program. Channel Assembly forgets aired programs about 15 minutes
after they end (channel-assembly.md §5.6); the guide shows 6 hours of history, so 30 days only serve tracing."""

type ItemKey = dict[str, str]


class AssetStatus(StrEnum):
    INGESTING = "INGESTING"
    ENCODED = "ENCODED"
    READY = "READY"
    FAILED = "FAILED"


class LadderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    FAILED = "FAILED"


class ProgramStatus(StrEnum):
    PLANNED = "PLANNED"
    PUBLISHED = "PUBLISHED"
    AIRED = "AIRED"
    SKIPPED = "SKIPPED"
    """Does not air: its start had passed when the stopped channel was started, which removes it from Channel
    Assembly."""


# --- time ------------------------------------------------------------------------------------------------------------


def now_iso() -> str:
    """Current time as ISO-8601 UTC with second precision (``2026-10-16T12:00:00Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(moment: int | datetime) -> str:
    """Epoch milliseconds or an aware datetime as ISO-8601 UTC with milliseconds (``2026-10-16T12:00:00.000Z``)."""
    instant = datetime.fromtimestamp(moment / 1000, UTC) if isinstance(moment, int) else moment.astimezone(UTC)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def expires_at(seconds: int) -> int:
    """TTL value for an item that DynamoDB may delete ``seconds`` from now."""
    return int(time.time()) + seconds


def program_expires_at(end_ms: int) -> int:
    """TTL value of a program's EPG item: :data:`PROGRAM_RETENTION_S` after the program ends."""
    return end_ms // 1000 + PROGRAM_RETENTION_S


# --- keys and index attributes -------------------------------------------------------------------------------------


def asset_key(asset_id: str, ladder_version: str) -> ItemKey:
    return {"pk": f"{ASSET_PREFIX}{asset_id}", "sk": f"LADDER#{ladder_version}"}


def asset_index(kind: str, ladder_version: str, status: AssetStatus, updated_at: str) -> ItemKey:
    """GSI1 attributes of an asset: assets of one kind and ladder, by status and recency."""
    return {"gsi1pk": f"KIND#{kind}#{ladder_version}", "gsi1sk": f"{status}#{updated_at}"}


def ladder_key(ladder_version: str) -> ItemKey:
    """Item key of a ladder version (``core.naming.ladder_path`` is the name-safe string form of the version)."""
    return {"pk": f"LADDER#{ladder_version.partition('@')[0]}", "sk": f"V#{ladder_version}"}


def ladder_index(status: LadderStatus, ladder_version: str) -> ItemKey:
    """GSI1 attributes of a ladder version: every ladder, by status."""
    return {"gsi1pk": "LADDERS", "gsi1sk": f"{status}#{ladder_version}"}


def channel_key(channel: str) -> ItemKey:
    return {"pk": f"{CHANNEL_PREFIX}{channel}", "sk": "META"}


def channel_index(owner: Owner, channel: str) -> ItemKey:
    """GSI1 attributes of a channel: every channel, by owner."""
    return {"gsi1pk": "CHANNELS", "gsi1sk": f"{owner}#{channel}"}


def program_sk(start_ms: int) -> str:
    """Sort key of a program item; zero-padded so that sort order is start order."""
    return f"{PROGRAM_PREFIX}{start_ms:013d}"


def program_key(channel: str, start_ms: int) -> ItemKey:
    return {"pk": f"{CHANNEL_PREFIX}{channel}", "sk": program_sk(start_ms)}


def program_index(program: str, channel: str) -> ItemKey:
    """GSI1 attributes of a program: looked up by its Channel Assembly name (the as-run log names programs)."""
    return {"gsi1pk": f"{PROGRAM_PREFIX}{program}", "gsi1sk": channel}


def plan_key(channel: str, plan_id: str) -> ItemKey:
    return {"pk": f"{CHANNEL_PREFIX}{channel}", "sk": f"PLAN#{plan_id}"}


def lease_key(channel: str) -> ItemKey:
    return {"pk": f"{CHANNEL_PREFIX}{channel}", "sk": "LEASE"}


def proposal_key(proposal_id: str) -> ItemKey:
    return {"pk": f"PROPOSAL#{proposal_id}", "sk": "META"}


# --- attribute codecs ----------------------------------------------------------------------------------------------


def to_dynamo(value: Any) -> Any:
    """``value`` with floats as Decimal, recursively: the DynamoDB resource API rejects floats."""
    match value:
        case float():
            return Decimal(str(value))
        case Mapping():
            return {k: to_dynamo(v) for k, v in value.items()}
        case list() | tuple():
            return [to_dynamo(v) for v in value]
        case _:
            return value


def from_dynamo(value: Any) -> Any:
    """``value`` read through the resource API with its Decimals as int (when integral) or float, recursively."""
    match value:
        case Decimal():
            return int(value) if value == value.to_integral_value() else float(value)
        case Mapping():
            return {k: from_dynamo(v) for k, v in value.items()}
        case list() | tuple() | set() | frozenset():
            return [from_dynamo(v) for v in value]
        case _:
            return value


def update_request(
    key: ItemKey, values: Mapping[str, Any], *, remove: Sequence[str] = (), **condition: Any
) -> dict[str, Any]:
    """``UpdateItem`` keyword arguments (resource API) that SET every attribute in ``values`` and REMOVE those in
    ``remove``.

    Every name is aliased (``name``, ``owner`` and ``status`` are reserved words). The placeholders are
    ``#f<n>``/``:f<n>`` and ``#r<n>``, so a boto3 condition object (``ConditionExpression=Attr(...)``, whose
    placeholders are ``#n<n>``/``:v<n>``) can be passed in ``condition``.
    """
    names = {f"#f{i}": name for i, name in enumerate(values)}
    removed = {f"#r{i}": name for i, name in enumerate(remove)}
    clauses = []
    if names:
        clauses.append("SET " + ", ".join(f"{placeholder} = :{placeholder[1:]}" for placeholder in names))
    if removed:
        clauses.append("REMOVE " + ", ".join(removed))
    request: dict[str, Any] = {
        "Key": key,
        "UpdateExpression": " ".join(clauses),
        "ExpressionAttributeNames": names | removed,
        **condition,
    }
    if values:
        request["ExpressionAttributeValues"] = {f":f{i}": to_dynamo(value) for i, value in enumerate(values.values())}
    return request
