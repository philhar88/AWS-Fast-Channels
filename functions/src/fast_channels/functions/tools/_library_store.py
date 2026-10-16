"""Typed access to the ``MediaLibrary`` table (CONTRACTS §D), shared by every agent tool target.

Records are pydantic models whose fields are the item's snake_case attributes. Key attributes (``pk``, ``sk``,
``gsi1pk``, ``gsi1sk``) are derived from the record, never stored on it, and a record read back from an item that
another component wrote (ingest pipeline, custom resources) recovers its identity from the keys. Timestamps are
ISO-8601 UTC strings; ``expires_at`` is epoch seconds (the table's TTL attribute).

Plans, leases and ladder proposals have no shared record model: their owners use :func:`plan_key`,
:func:`lease_key` and :func:`proposal_key` with :func:`put_item`.
"""

import base64
import binascii
import json
import operator
import time
from abc import abstractmethod
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import reduce
from typing import TYPE_CHECKING, Any, Final, Self

from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.ingest import AssetKind  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.core.ladder import Ladder
from fast_channels.core.timeline import PlannedProgram, ProgrammingBrief
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools.registry import ToolError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

GSI1: Final = "GSI1"
TTL_ATTRIBUTE: Final = "expires_at"
TABLE_KEYS: Final = ("pk", "sk")
GSI1_KEYS: Final = ("pk", "sk", "gsi1pk", "gsi1sk")
"""ExclusiveStartKey attributes of a base-table read and of a GSI1 query."""
_MAX_EPOCH_MS: Final = 10**13 - 1  # program sort keys are zero-padded to 13 digits

type Item = dict[str, Any]
type ItemKey = dict[str, str]


class Owner(StrEnum):
    """Who manages a resource (the ``fast:owner`` tag): CloudFormation, the ingest pipeline or the agent."""

    IAC = "iac"
    PIPELINE = "pipeline"
    AGENT = "agent"


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


# --- keys ----------------------------------------------------------------------------------------------------------


def asset_key(asset_id: str, ladder_version: str) -> ItemKey:
    return {"pk": f"ASSET#{asset_id}", "sk": f"LADDER#{ladder_version}"}


def ladder_key(ladder_version: str) -> ItemKey:
    return {"pk": f"LADDER#{ladder_version.partition('@')[0]}", "sk": f"V#{ladder_version}"}


def channel_key(channel: str) -> ItemKey:
    return {"pk": f"CHANNEL#{channel}", "sk": "META"}


def program_key(channel: str, start_ms: int) -> ItemKey:
    return {"pk": f"CHANNEL#{channel}", "sk": f"PROGRAM#{start_ms:013d}"}


def plan_key(channel: str, plan_id: str) -> ItemKey:
    return {"pk": f"CHANNEL#{channel}", "sk": f"PLAN#{plan_id}"}


def lease_key(channel: str) -> ItemKey:
    return {"pk": f"CHANNEL#{channel}", "sk": "LEASE"}


def proposal_key(proposal_id: str) -> ItemKey:
    return {"pk": f"PROPOSAL#{proposal_id}", "sk": "META"}


def _suffix(item: Mapping[str, Any], attribute: str, prefix: str) -> str | None:
    value = item.get(attribute)
    return value.removeprefix(prefix) if isinstance(value, str) and value.startswith(prefix) else None


def _known(**fields: str | None) -> dict[str, str]:
    return {name: value for name, value in fields.items() if value is not None}


# --- records -------------------------------------------------------------------------------------------------------


class Record(BaseModel):
    """A MediaLibrary item. Unknown attributes (written by newer components) are ignored."""

    model_config = ConfigDict(extra="ignore")

    updated_at: str = Field(default_factory=now_iso, description="Last write (ISO-8601 UTC); set by put().")

    @abstractmethod
    def key(self) -> ItemKey:
        """Primary key (``pk``, ``sk``)."""

    @abstractmethod
    def index(self) -> ItemKey:
        """GSI1 key (``gsi1pk``, ``gsi1sk``)."""

    @classmethod
    @abstractmethod
    def identity(cls, item: Mapping[str, Any]) -> dict[str, str]:
        """Fields recoverable from the keys of ``item`` (attributes of the same name take precedence)."""

    def to_item(self) -> Item:
        """The DynamoDB item: JSON-mode attributes (floats as Decimal, None omitted) plus key attributes."""
        return {**decimals(self.model_dump(mode="json", exclude_none=True)), **self.key(), **self.index()}

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> Self:
        return cls.model_validate({**cls.identity(item), **plain(item)})


class Asset(Record):
    """``ASSET#<asset_id>`` / ``LADDER#<ladder_version>``: one encode (or registered external rendition set)."""

    asset_id: str
    ladder_version: str
    kind: AssetKind
    status: AssetStatus
    owner: Owner = Owner.PIPELINE
    title: str | None = None
    content_id: str | None = None
    duration_ms: int | None = None
    break_offsets_ms: list[int] = Field([], description="Offsets where every track has a common segment boundary.")
    signature: list[list[str]] = Field([], description="core.manifests.ladder_signature of its HLS manifest.")
    vod_source: str | None = Field(None, description="Channel Assembly VOD source name (none for ads).")
    source_location: str | None = Field(None, description="Source location of the VOD source; None = the deployment's.")
    packages: dict[str, str] = Field({}, description="Source group -> manifest path relative to the source location.")
    source_uri: str | None = None
    correlation_id: str | None = Field(None, description="Ingest execution name, or the registering agent session.")
    error: str | None = None

    def key(self) -> ItemKey:
        return asset_key(self.asset_id, self.ladder_version)

    def index(self) -> ItemKey:
        return {"gsi1pk": f"KIND#{self.kind}#{self.ladder_version}", "gsi1sk": f"{self.status}#{self.updated_at}"}

    @classmethod
    def identity(cls, item: Mapping[str, Any]) -> dict[str, str]:
        return _known(asset_id=_suffix(item, "pk", "ASSET#"), ladder_version=_suffix(item, "sk", "LADDER#"))


class LadderKit(BaseModel):
    """The VOD sources and creatives provisioned for a ladder version (SPEC §5.2)."""

    model_config = ConfigDict(extra="ignore")

    filler: str | None = Field(None, description="VOD source of the filler slate.")
    break_slates: dict[int, str] = Field({}, description="Slate length in GOPs -> VOD source (core.specs.ad_breaks).")
    house_ads: list[str] = Field([], description="Creative ids of the house ads in the VAST pod.")
    vast_key: str | None = Field(None, description="Media-bucket key of the static VAST pod.")


class LadderRecord(Record):
    """``LADDER#<ladder_id>`` / ``V#<ladder_version>``: a ladder version, its job template and its kit."""

    ladder_version: str
    status: LadderStatus
    ladder: Ladder | None = Field(None, description="The definition; None for a built-in ladder (Ladder.builtin).")
    description: str | None = None
    job_template: str | None = None
    kit: LadderKit = LadderKit()
    owner: Owner = Owner.IAC
    correlation_id: str | None = None
    error: str | None = None

    @property
    def ladder_id(self) -> str:
        return self.ladder_version.partition("@")[0]

    def definition(self) -> Ladder:
        """The stored ladder, else the built-in ladder of this version; raises ToolError NOT_FOUND."""
        if ladder := self.ladder or builtin_ladder(self.ladder_version):
            return ladder
        raise _unknown_ladder(self.ladder_version)

    def key(self) -> ItemKey:
        return ladder_key(self.ladder_version)

    def index(self) -> ItemKey:
        return {"gsi1pk": "LADDERS", "gsi1sk": f"{self.status}#{self.ladder_version}"}

    @classmethod
    def identity(cls, item: Mapping[str, Any]) -> dict[str, str]:
        return _known(ladder_version=_suffix(item, "sk", "V#"))


class ChannelRecord(Record):
    """``CHANNEL#<name>`` / ``META``: a linear channel and its playback configuration."""

    name: str
    ladder_version: str
    outputs: list[str] = Field(description="Output (= source group = rendition profile) names.")
    owner: Owner
    tier: str | None = None
    arn: str | None = None
    playback_configuration_name: str | None = None
    hls_prefix: str | None = Field(None, description="Playback config HlsConfiguration.ManifestEndpointPrefix.")
    dash_prefix: str | None = Field(None, description="Playback config DashConfiguration.ManifestEndpointPrefix.")
    brief: ProgrammingBrief | None = None
    correlation_id: str | None = None

    def key(self) -> ItemKey:
        return channel_key(self.name)

    def index(self) -> ItemKey:
        return {"gsi1pk": "CHANNELS", "gsi1sk": f"{self.owner}#{self.name}"}

    @classmethod
    def identity(cls, item: Mapping[str, Any]) -> dict[str, str]:
        return _known(name=_suffix(item, "pk", "CHANNEL#"))


class ProgramRecord(Record, PlannedProgram):
    """``CHANNEL#<name>`` / ``PROGRAM#<start_ms:013d>``: a scheduled program (the EPG source of truth)."""

    channel: str
    status: ProgramStatus
    content_id: str | None = None  # optional here: to_item omits None attributes
    plan_id: str | None = None
    source_location: str | None = Field(None, description="Source location of vod_source; None = the deployment's.")
    title: str | None = None
    rating: str | None = None
    genres: list[str] = []
    correlation_id: str | None = None

    def key(self) -> ItemKey:
        return program_key(self.channel, self.start_ms)

    def index(self) -> ItemKey:
        return {"gsi1pk": f"PROGRAM#{self.name}", "gsi1sk": self.channel}

    @classmethod
    def identity(cls, item: Mapping[str, Any]) -> dict[str, str]:
        return _known(channel=_suffix(item, "pk", "CHANNEL#"))


@dataclass(frozen=True, slots=True)
class Page[T]:
    """Up to ``limit`` results; ``next_token`` is set when more may follow."""

    items: list[T]
    next_token: str | None = None


# --- table access --------------------------------------------------------------------------------------------------


def table() -> Table:
    config = settings()
    config.require("library_table")
    return aws.table(config.library_table)


def get_item(key: ItemKey) -> Item | None:
    """The raw item at ``key`` (numbers as int/float), or None."""
    item = table().get_item(Key=key).get("Item")
    return plain(item) if item else None


def put_item(item: Mapping[str, Any], *, condition: ConditionBase | None = None) -> None:
    """Write a raw item (floats become Decimal); a failed ``condition`` raises ConditionalCheckFailedException."""
    request: dict[str, Any] = {"Item": decimals(item)}
    if condition is not None:
        request["ConditionExpression"] = condition
    table().put_item(**request)


def delete_item(key: ItemKey) -> None:
    table().delete_item(Key=key)


def get[R: Record](record: type[R], key: ItemKey) -> R | None:
    item = table().get_item(Key=key).get("Item")
    return record.from_item(item) if item else None


def put[R: Record](record: R, *, condition: ConditionBase | None = None) -> R:
    """Write ``record`` with a fresh ``updated_at`` and return the stored version.

    ``condition`` guards the write, e.g. ``Attr("pk").not_exists()`` for create-only; a failed condition raises
    ConditionalCheckFailedException, which the tool dispatcher reports as CONFLICT.
    """
    stored = record.model_copy(update={"updated_at": now_iso()})
    request: dict[str, Any] = {"Item": stored.to_item()}
    if condition is not None:
        request["ConditionExpression"] = condition
    table().put_item(**request)
    return stored


def query_all(**request: Any) -> list[Item]:
    """Every item of a Query (all pages)."""
    items: list[Item] = []
    while True:
        page = table().query(**request)
        items += page.get("Items", [])
        if not (start := page.get("LastEvaluatedKey")):
            return items
        request["ExclusiveStartKey"] = start


def get_asset(asset_id: str, ladder_version: str) -> Asset | None:
    return get(Asset, asset_key(asset_id, ladder_version))


def asset_versions(asset_id: str) -> list[Asset]:
    """Every encode of ``asset_id``, one per ladder version."""
    return [Asset.from_item(i) for i in query_all(KeyConditionExpression=Key("pk").eq(f"ASSET#{asset_id}"))]


def find_assets(
    *,
    kind: AssetKind | None = None,
    status: AssetStatus | None = None,
    ladder_version: str | None = None,
    content_id: str | None = None,
    limit: int = 50,
    next_token: str | None = None,
) -> Page[Asset]:
    """Assets matching every given filter.

    With ``kind`` and ``ladder_version`` this is a GSI1 query (newest first within each status); otherwise a
    filtered scan, which suits a library of at most a few thousand encodes (1,000 VOD sources per source location).
    """
    if kind is not None and ladder_version is not None:
        condition: ConditionBase = Key("gsi1pk").eq(f"KIND#{kind}#{ladder_version}")
        if status is not None:
            condition &= Key("gsi1sk").begins_with(f"{status}#")
        request: dict[str, Any] = {"IndexName": GSI1, "KeyConditionExpression": condition, "ScanIndexForward": False}
        if content_id is not None:
            request["FilterExpression"] = Attr("content_id").eq(content_id)
        items, token = collect(table().query, request, limit=limit, next_token=next_token, key_names=GSI1_KEYS)
    else:
        filters = [
            Attr(name).eq(value)
            for name, value in (
                ("kind", kind),
                ("status", status),
                ("sk", ladder_version and f"LADDER#{ladder_version}"),
                ("content_id", content_id),
            )
            if value is not None
        ]
        request = {"FilterExpression": _all_of([Attr("pk").begins_with("ASSET#"), *filters])}
        items, token = collect(table().scan, request, limit=limit, next_token=next_token, key_names=TABLE_KEYS)
    return Page([Asset.from_item(i) for i in items], token)


def get_ladder(ladder_version: str) -> LadderRecord | None:
    return get(LadderRecord, ladder_key(ladder_version))


def list_ladders(status: LadderStatus | None = None) -> list[LadderRecord]:
    """Every ladder version (optionally only those in ``status``), ordered by status then version."""
    condition: ConditionBase = Key("gsi1pk").eq("LADDERS")
    if status is not None:
        condition &= Key("gsi1sk").begins_with(f"{status}#")
    return [LadderRecord.from_item(i) for i in query_all(IndexName=GSI1, KeyConditionExpression=condition)]


def ladder_definition(ladder_version: str) -> Ladder:
    """The ladder a version names: the stored definition, else the built-in ladder with that id and version.

    Raises ToolError NOT_FOUND for an unknown version.
    """
    if (record := get_ladder(ladder_version)) is not None:
        return record.definition()
    if ladder := builtin_ladder(ladder_version):
        return ladder
    raise _unknown_ladder(ladder_version)


def builtin_ladder(ladder_version: str) -> Ladder | None:
    """The built-in ladder (``ladders/<id>.toml``) whose version is exactly ``ladder_version``, if any."""
    try:
        ladder = Ladder.builtin(ladder_version.partition("@")[0])
    except OSError, ValueError:
        return None
    return ladder if ladder.version == ladder_version else None


def _unknown_ladder(ladder_version: str) -> ToolError:
    return ToolError(
        "NOT_FOUND",
        f"unknown ladder version {ladder_version}",
        hint="Call encoding___list_ladders for the provisioned ladder versions.",
    )


def get_channel(name: str) -> ChannelRecord | None:
    return get(ChannelRecord, channel_key(name))


def list_channels(owner: Owner | None = None) -> list[ChannelRecord]:
    """Every channel (optionally only those of ``owner``), ordered by owner then name."""
    condition: ConditionBase = Key("gsi1pk").eq("CHANNELS")
    if owner is not None:
        condition &= Key("gsi1sk").begins_with(f"{owner}#")
    return [ChannelRecord.from_item(i) for i in query_all(IndexName=GSI1, KeyConditionExpression=condition)]


def programs(channel: str, *, start_ms: int | None = None, end_ms: int | None = None) -> list[ProgramRecord]:
    """Programs of ``channel`` that start in ``[start_ms, end_ms]`` (either bound optional), in start order."""
    low = 0 if start_ms is None else start_ms
    high = _MAX_EPOCH_MS if end_ms is None else end_ms
    condition = Key("pk").eq(f"CHANNEL#{channel}") & Key("sk").between(f"PROGRAM#{low:013d}", f"PROGRAM#{high:013d}")
    return [ProgramRecord.from_item(i) for i in query_all(KeyConditionExpression=condition)]


def program_by_name(name: str) -> ProgramRecord | None:
    items = query_all(IndexName=GSI1, KeyConditionExpression=Key("gsi1pk").eq(f"PROGRAM#{name}"))
    return ProgramRecord.from_item(items[0]) if items else None


# --- paging (any table) --------------------------------------------------------------------------------------------


def collect(
    call: Callable[..., Mapping[str, Any]],
    request: Mapping[str, Any],
    *,
    limit: int,
    next_token: str | None,
    key_names: Sequence[str],
    keep: Callable[[Item], bool] | None = None,
) -> tuple[list[Item], str | None]:
    """Up to ``limit`` items of a Scan or Query (``call``) that pass ``keep``, and the token of the next page.

    DynamoDB applies ``Limit`` before filtering, so this reads pages until ``limit`` matches are found and resumes
    after the last returned item; the token is that item's key (``key_names``: table keys, plus index keys for an
    index query). Items are returned with plain numbers.
    """
    start = decode_token(next_token, key_names)
    found: list[Item] = []
    while True:
        page = call(**request, **({"ExclusiveStartKey": start} if start else {}))
        items = [plain(i) for i in page.get("Items", [])]
        more = "LastEvaluatedKey" in page
        for position, item in enumerate(items, start=1):
            if keep is not None and not keep(item):
                continue
            found.append(item)
            if len(found) == limit:
                remaining = more or position < len(items)
                return found, encode_token({k: item[k] for k in key_names}) if remaining else None
        if not more:
            return found, None
        start = page["LastEvaluatedKey"]


def encode_token(key: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(key, separators=(",", ":")).encode()).decode()


def decode_token(token: str | None, key_names: Sequence[str]) -> Item | None:
    """The ExclusiveStartKey in ``token``; raises ToolError VALIDATION if it is not a token of this kind of query."""
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
    except binascii.Error, ValueError:
        key = None
    if not isinstance(key, dict) or set(key) != set(key_names):
        raise ToolError(
            "VALIDATION", "next_token is invalid", hint="Pass next_token from the previous page of the same query."
        )
    return key


def decimals(value: Any) -> Any:
    """``value`` with floats as Decimal, recursively: the DynamoDB resource API rejects floats."""
    match value:
        case float():
            return Decimal(str(value))
        case Mapping():
            return {k: decimals(v) for k, v in value.items()}
        case list() | tuple():
            return [decimals(v) for v in value]
        case _:
            return value


def plain(value: Any) -> Any:
    """``value`` with DynamoDB Decimals as int (when integral) or float, recursively."""
    match value:
        case Decimal():
            return int(value) if value == value.to_integral_value() else float(value)
        case Mapping():
            return {k: plain(v) for k, v in value.items()}
        case list() | tuple():
            return [plain(v) for v in value]
        case _:
            return value


def _all_of(conditions: Iterable[ConditionBase]) -> ConditionBase:
    return reduce(operator.and_, conditions)
