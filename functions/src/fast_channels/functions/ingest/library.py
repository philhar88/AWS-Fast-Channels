"""Media-library items the ingest pipeline writes (CONTRACTS §D) and the names it gives runtime resources.

Pure: keys, attributes, names and request builders; the handlers do the I/O. Attribute names and shapes are the
ones the agent tools read (``fast_channels.functions.tools._library_store``): snake_case attributes, ISO-8601 UTC
``updated_at``, the ladder definition stored as a map (``Ladder.model_dump(mode="json")``) and the kit's break
slates keyed by their length in GOPs.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from fast_channels.core.naming import ladder_key as ladder_path
from fast_channels.core.naming import mediatailor_name

OWNER: Final = "pipeline"
"""``fast:owner`` of every resource the pipeline creates (and ``owner`` of its asset items)."""
PARENT_EXECUTION_KEY: Final = "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID"
"""Input key Step Functions adds to child executions started with ``associate_with_parent`` (ladder kits)."""

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


def now_iso() -> str:
    """Current time as ISO-8601 UTC with second precision (``2026-10-16T12:00:00Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def asset_key(asset_id: str, ladder_version: str) -> ItemKey:
    return {"pk": f"ASSET#{asset_id}", "sk": f"LADDER#{ladder_version}"}


def asset_index(kind: str, ladder_version: str, status: AssetStatus, updated_at: str) -> ItemKey:
    """GSI1 attributes of an asset: assets of one kind and ladder, by status and recency."""
    return {"gsi1pk": f"KIND#{kind}#{ladder_version}", "gsi1sk": f"{status}#{updated_at}"}


def ladder_key(ladder_version: str) -> ItemKey:
    return {"pk": f"LADDER#{ladder_version.partition('@')[0]}", "sk": f"V#{ladder_version}"}


def ladder_index(status: LadderStatus, ladder_version: str) -> ItemKey:
    """GSI1 attributes of a ladder version: every ladder, by status."""
    return {"gsi1pk": "LADDERS", "gsi1sk": f"{status}#{ladder_version}"}


def job_template_name(prefix: str, ladder_version: str) -> str:
    """MediaConvert job template of a ladder version: ``<prefix>-<ladder id>_<fingerprint>``."""
    return f"{prefix}-{ladder_path(ladder_version)}"


def vod_source_name(asset_id: str, ladder_version: str) -> str:
    """Channel Assembly VOD source of an asset encode: unique per asset and ladder within the source location."""
    return mediatailor_name(asset_id, ladder_path(ladder_version))


def runtime_tags(prefix: str, *, ladder_version: str, correlation_id: str, **extra: str) -> dict[str, str]:
    """Tags of a runtime resource the pipeline creates (CONTRACTS §E); the reaper finds them by ``fast:owner``."""
    return {
        "fast:app": prefix,
        "fast:owner": OWNER,
        "fast:ladder": ladder_version,
        "fast:correlation-id": correlation_id,
        **{f"fast:{name.replace('_', '-')}": value for name, value in extra.items()},
    }


def to_dynamo(value: Any) -> Any:
    """``value`` with floats as Decimal, which the DynamoDB resource API requires."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def from_dynamo(value: Any) -> Any:
    """``value`` read through the resource API with its Decimals back as int or float."""
    match value:
        case Decimal():
            return int(value) if value == value.to_integral_value() else float(value)
        case Mapping():
            return {k: from_dynamo(v) for k, v in value.items()}
        case list() | set():
            return [from_dynamo(v) for v in value]
        case _:
            return value


def update_request(
    key: ItemKey, values: Mapping[str, Any], *, remove: Sequence[str] = (), **condition: Any
) -> dict[str, Any]:
    """``UpdateItem`` kwargs (resource API) that SET every attribute in ``values`` and REMOVE those in ``remove``.

    Placeholders are ``#f<n>``/``:f<n>`` and ``#r<n>``, so a boto3 condition object
    (``ConditionExpression=Attr(...)``, whose placeholders are ``#n``/``:v``) can be passed in ``condition``.
    """
    names = {f"#f{i}": name for i, name in enumerate(values)}
    removed = {f"#r{i}": name for i, name in enumerate(remove)}
    expression = "SET " + ", ".join(f"{placeholder} = :{placeholder[1:]}" for placeholder in names)
    if removed:
        expression += " REMOVE " + ", ".join(removed)
    return {
        "Key": key,
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names | removed,
        "ExpressionAttributeValues": {f":f{i}": to_dynamo(value) for i, value in enumerate(values.values())},
        **condition,
    }
