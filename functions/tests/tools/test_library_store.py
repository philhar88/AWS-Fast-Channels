import base64
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from fast_channels.core.ingest import AssetKind
from fast_channels.core.ladder import Ladder
from fast_channels.core.timeline import PlannedBreak
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools.registry import ToolError

TABLE = "library-test"
NOW = "2026-10-16T12:00:00Z"
STANDARD = Ladder.builtin("standard")
_serialize = TypeSerializer().serialize


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    """An item as the low-level client returns it (the Table resource deserialises responses after the stub)."""
    return {k: _serialize(v) for k, v in item.items()}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LIBRARY_TABLE", TABLE)
    monkeypatch.setattr(store, "now_iso", lambda: NOW)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def ddb() -> Iterator[Stubber]:
    with Stubber(aws.table(TABLE).meta.client) as stub:
        yield stub
        stub.assert_no_pending_responses()


def asset(**changes: Any) -> store.Asset:
    fields: dict[str, Any] = {
        "asset_id": "bbb-1a2b3c",
        "ladder_version": STANDARD.version,
        "kind": "content",
        "status": "READY",
        "duration_ms": 596_462,
        "break_offsets_ms": [6006, 12012],
        "vod_source": "fast-bbb",
        "updated_at": NOW,
    }
    return store.Asset(**fields | changes)


# --- records -------------------------------------------------------------------------------------------------------


def test_asset_item_has_contract_keys_and_round_trips() -> None:
    item = asset().to_item()
    assert item["pk"] == "ASSET#bbb-1a2b3c"
    assert item["sk"] == f"LADDER#{STANDARD.version}"
    assert item["gsi1pk"] == f"KIND#content#{STANDARD.version}"
    assert item["gsi1sk"] == f"READY#{NOW}"
    assert "title" not in item  # None attributes are omitted
    assert store.Asset.from_item(item) == asset()


def test_items_written_by_other_components_recover_identity_from_keys() -> None:
    item = {
        "pk": "ASSET#slate-15g",
        "sk": f"LADDER#{STANDARD.version}",
        "kind": "slate",
        "status": "READY",
        "duration_ms": Decimal(30030),
        "future_attribute": "ignored",
    }
    record = store.Asset.from_item(item)
    assert (record.asset_id, record.ladder_version, record.duration_ms) == ("slate-15g", STANDARD.version, 30030)
    assert record.owner is store.Owner.PIPELINE


def test_ladder_record_round_trips_definition_and_kit() -> None:
    record = store.LadderRecord(
        ladder_version=STANDARD.version,
        status="READY",
        ladder=STANDARD,
        kit=store.LadderKit(filler="fast-filler", break_slates={15: "fast-slate-15"}),
    )
    item = record.to_item()
    assert item["pk"] == "LADDER#standard" and item["sk"] == f"V#{STANDARD.version}"
    assert item["gsi1sk"] == f"READY#{STANDARD.version}"
    assert item["kit"]["break_slates"] == {"15": "fast-slate-15"}
    assert isinstance(item["ladder"]["gop_seconds"], Decimal)  # floats must reach DynamoDB as Decimal
    restored = store.LadderRecord.from_item(item)
    assert restored.kit.break_slates == {15: "fast-slate-15"}
    assert restored.ladder is not None and restored.ladder.version == STANDARD.version
    assert restored.ladder_id == "standard"


def test_a_definition_from_an_older_ladder_model_reads_as_none() -> None:
    item = store.LadderRecord(ladder_version="studio@0123456789", status="READY", ladder=STANDARD).to_item()
    item["ladder"]["video"][0]["codec"] = "hevc"  # a rung of the multi-codec CMAF model
    item["ladder"]["captions"] = [{"language": "en", "label": "English", "kind": "captions"}]

    restored = store.LadderRecord.from_item(item)

    assert restored.ladder is None and restored.status == "READY"
    assert store.definition(restored) is None


def test_channel_and_program_keys() -> None:
    channel = store.ChannelRecord(name="fast-demo", ladder_version=STANDARD.version, outputs=["hls"], owner="iac")
    assert channel.key() == {"pk": "CHANNEL#fast-demo", "sk": "META"}
    assert channel.index() == {"gsi1pk": "CHANNELS", "gsi1sk": "iac#fast-demo"}

    program = store.ProgramRecord(
        channel="fast-demo",
        status="PUBLISHED",
        name="20261016T120000-bbb",
        asset_id="bbb",
        vod_source="fast-bbb",
        start_ms=1_792_116_000_000,
        duration_ms=600_600,
        breaks=[PlannedBreak(offset_ms=300_300, duration_ms=60_060, slate_gops=30, event_id=7, upid="4653")],
        ladder_version=STANDARD.version,
    )
    item = program.to_item()
    assert item["sk"] == "PROGRAM#1792116000000"
    assert item["gsi1pk"] == "PROGRAM#20261016T120000-bbb" and item["gsi1sk"] == "fast-demo"
    assert store.ProgramRecord.from_item({k: v for k, v in item.items() if k != "channel"}) == program
    assert program.end_ms == 1_792_116_600_600


def test_entity_keys() -> None:
    assert store.program_key("c", 5) == {"pk": "CHANNEL#c", "sk": "PROGRAM#0000000000005"}
    assert store.plan_key("c", "p1") == {"pk": "CHANNEL#c", "sk": "PLAN#p1"}
    assert store.lease_key("c") == {"pk": "CHANNEL#c", "sk": "LEASE"}
    assert store.proposal_key("x") == {"pk": "PROPOSAL#x", "sk": "META"}
    assert store.ladder_key("lite@0123456789") == {"pk": "LADDER#lite", "sk": "V#lite@0123456789"}


def test_time_helpers() -> None:
    assert store.iso(1_792_116_463_186) == "2026-10-16T02:07:43.186Z"
    eastern = datetime(2026, 10, 16, 8, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert store.iso(eastern) == "2026-10-16T12:00:00.000Z"
    assert store.expires_at(3600) - int(datetime.now(UTC).timestamp()) in {3599, 3600}


def test_plain_converts_decimals_recursively() -> None:
    assert store.plain({"a": [Decimal(2), Decimal("2.5")], "b": {"c": Decimal(0)}, "d": "x"}) == {
        "a": [2, 2.5],
        "b": {"c": 0},
        "d": "x",
    }


# --- ladder definitions --------------------------------------------------------------------------------------------


def _get(stub: Stubber, key: dict[str, str], item: dict[str, Any] | None) -> None:
    stub.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": TABLE, "Key": key})


def test_ladder_definition_prefers_the_stored_definition(ddb: Stubber) -> None:
    custom = STANDARD.model_copy(update={"id": "custom"})
    version = f"custom@{STANDARD.fingerprint}"
    record = store.LadderRecord(ladder_version=version, status="READY", ladder=custom)
    _get(ddb, store.ladder_key(version), record.to_item())
    assert store.ladder_definition(version).id == "custom"


def test_ladder_definition_falls_back_to_builtin_ladders(ddb: Stubber) -> None:
    _get(ddb, store.ladder_key(STANDARD.version), {**store.ladder_key(STANDARD.version), "status": "READY"})
    assert store.ladder_definition(STANDARD.version) == STANDARD


@pytest.mark.parametrize("version", ["standard@0000000000", "nosuch@0000000000"])
def test_unknown_ladder_versions_are_not_found(ddb: Stubber, version: str) -> None:
    _get(ddb, store.ladder_key(version), None)
    with pytest.raises(ToolError) as raised:
        store.ladder_definition(version)
    assert raised.value.code == "NOT_FOUND"


# --- table access --------------------------------------------------------------------------------------------------


def test_put_stamps_updated_at_and_applies_the_condition(ddb: Stubber) -> None:
    record = asset(updated_at="2020-01-01T00:00:00Z")
    expected = {"TableName": TABLE, "Item": asset().to_item(), "ConditionExpression": Attr("pk").not_exists()}
    ddb.add_response("put_item", {}, expected)
    assert store.put(record, condition=Attr("pk").not_exists()).updated_at == NOW


def test_get_returns_none_for_missing_items(ddb: Stubber) -> None:
    _get(ddb, store.asset_key("x", STANDARD.version), None)
    assert store.get_asset("x", STANDARD.version) is None


def test_raw_items(ddb: Stubber) -> None:
    key = store.plan_key("c", "p1")
    written = {**key, "score": Decimal("0.5"), "n": Decimal(3)}
    ddb.add_response("put_item", {}, {"TableName": TABLE, "Item": written})
    _get(ddb, key, {**key, "score": Decimal("0.5"), "n": Decimal(3)})
    ddb.add_response("delete_item", {}, {"TableName": TABLE, "Key": key})
    store.put_item({**key, "score": 0.5, "n": Decimal(3)})  # floats and stored Decimals both work
    assert store.get_item(key) == {**key, "score": 0.5, "n": 3}
    store.delete_item(key)


def test_asset_versions_reads_every_page(ddb: Stubber) -> None:
    first, second = asset(), asset(ladder_version="lite@5306b91dd0")
    query = {"TableName": TABLE, "KeyConditionExpression": Key("pk").eq("ASSET#bbb-1a2b3c")}
    start = first.key()
    ddb.add_response("query", {"Items": [dynamo(first.to_item())], "LastEvaluatedKey": dynamo(start)}, query)
    ddb.add_response("query", {"Items": [dynamo(second.to_item())]}, {**query, "ExclusiveStartKey": start})
    assert store.asset_versions("bbb-1a2b3c") == [first, second]


def test_find_assets_queries_gsi1_with_kind_and_ladder(ddb: Stubber) -> None:
    expected = {
        "TableName": TABLE,
        "IndexName": "GSI1",
        "KeyConditionExpression": Key("gsi1pk").eq(f"KIND#content#{STANDARD.version}")
        & Key("gsi1sk").begins_with("READY#"),
        "FilterExpression": Attr("content_id").eq("bbb"),
        "ScanIndexForward": False,
    }
    ddb.add_response("query", {"Items": [dynamo(asset().to_item())]}, expected)
    page = store.find_assets(
        kind=AssetKind.CONTENT, status=store.AssetStatus.READY, ladder_version=STANDARD.version, content_id="bbb"
    )
    assert page.items == [asset()] and page.next_token is None


def test_find_assets_scans_otherwise(ddb: Stubber) -> None:
    expected = {
        "TableName": TABLE,
        "FilterExpression": Attr("pk").begins_with("ASSET#")
        & Attr("status").eq("FAILED")
        & Attr("sk").eq(f"LADDER#{STANDARD.version}"),
    }
    failed = asset(status="FAILED")
    more = {"Items": [dynamo(failed.to_item())], "LastEvaluatedKey": dynamo(failed.key())}
    ddb.add_response("scan", more, expected)
    page = store.find_assets(status=store.AssetStatus.FAILED, ladder_version=STANDARD.version, limit=1)
    assert page.items == [failed]
    assert store.decode_token(page.next_token, store.TABLE_KEYS) == failed.key()


def test_index_queries(ddb: Stubber) -> None:
    channel = store.ChannelRecord(name="fast-demo", ladder_version=STANDARD.version, outputs=["hls"], owner="agent")
    ladder = store.LadderRecord(ladder_version=STANDARD.version, status="READY")

    def gsi(pk: str, prefix: str | None = None) -> dict[str, Any]:
        condition = Key("gsi1pk").eq(pk)
        return {
            "TableName": TABLE,
            "IndexName": "GSI1",
            "KeyConditionExpression": condition & Key("gsi1sk").begins_with(prefix) if prefix else condition,
        }

    ddb.add_response("query", {"Items": [dynamo(channel.to_item())]}, gsi("CHANNELS", "agent#"))
    ddb.add_response("query", {"Items": [dynamo(ladder.to_item())]}, gsi("LADDERS", "READY#"))
    ddb.add_response("query", {"Items": []}, gsi("PROGRAM#p"))
    assert store.list_channels(store.Owner.AGENT) == [channel]
    assert [r.ladder_version for r in store.list_ladders(store.LadderStatus.READY)] == [STANDARD.version]
    assert store.program_by_name("p") is None


def test_programs_query_a_start_range(ddb: Stubber) -> None:
    def between(low: str, high: str) -> dict[str, Any]:
        condition = Key("pk").eq("CHANNEL#c") & Key("sk").between(f"PROGRAM#{low}", f"PROGRAM#{high}")
        return {"TableName": TABLE, "KeyConditionExpression": condition}

    ddb.add_response("query", {"Items": []}, between("0000000000000", "9999999999999"))
    ddb.add_response("query", {"Items": []}, between("0000000001000", "0000000002000"))
    assert store.programs("c") == []
    assert store.programs("c", start_ms=1000, end_ms=2000) == []


# --- paging --------------------------------------------------------------------------------------------------------


class FakeTable:
    """A paged Scan: two pages of numbered items."""

    def __init__(self) -> None:
        self.pages = [
            [{"pk": f"K{n}", "sk": "M", "n": Decimal(n)} for n in range(5)],
            [{"pk": f"K{n}", "sk": "M", "n": Decimal(n)} for n in range(5, 8)],
        ]
        self.starts: list[Any] = []

    def scan(self, **request: Any) -> dict[str, Any]:
        start = request.get("ExclusiveStartKey")
        self.starts.append(start)
        rows = [item for page in self.pages for item in page]
        position = 0 if start is None else next(i for i, r in enumerate(rows) if r["pk"] == start["pk"]) + 1
        page_end = 5 if position < 5 else len(rows)
        page = rows[position:page_end]
        return {
            "Items": page,
            **({"LastEvaluatedKey": {"pk": page[-1]["pk"], "sk": "M"}} if page_end < len(rows) else {}),
        }


def test_collect_reads_pages_until_limit_matches() -> None:
    table = FakeTable()
    even = lambda item: item["n"] % 2 == 0  # noqa: E731
    items, token = store.collect(table.scan, {}, limit=3, next_token=None, key_names=("pk", "sk"), keep=even)
    assert [i["n"] for i in items] == [0, 2, 4]
    assert store.decode_token(token, ("pk", "sk")) == {"pk": "K4", "sk": "M"}

    rest, end = store.collect(table.scan, {}, limit=3, next_token=token, key_names=("pk", "sk"), keep=even)
    assert [i["n"] for i in rest] == [6] and end is None
    assert table.starts[-1] == {"pk": "K4", "sk": "M"}


def test_collect_omits_the_token_when_the_last_match_ends_the_table() -> None:
    items, token = store.collect(FakeTable().scan, {}, limit=8, next_token=None, key_names=("pk", "sk"))
    assert len(items) == 8 and token is None


@pytest.mark.parametrize(
    "token", ["not base64!", store.encode_token({"pk": "x"}), base64.urlsafe_b64encode(b"[1]").decode()]
)
def test_foreign_tokens_are_rejected(token: str) -> None:
    with pytest.raises(ToolError) as raised:
        store.decode_token(token, ("pk", "sk"))
    assert raised.value.code == "VALIDATION"


def test_more_table_access(ddb: Stubber) -> None:
    key = store.lease_key("c")
    ddb.add_response(
        "put_item",
        {},
        {"TableName": TABLE, "Item": {**key, "at": [Decimal("1.5")]}, "ConditionExpression": Attr("pk").not_exists()},
    )
    store.put_item({**key, "at": [1.5]}, condition=Attr("pk").not_exists())

    channel = store.ChannelRecord(name="c", ladder_version=STANDARD.version, outputs=["hls"], owner="iac")
    _get(ddb, store.channel_key("c"), channel.to_item())
    assert store.get_channel("c") == channel

    _get(ddb, store.ladder_key(STANDARD.version), None)
    assert store.ladder_definition(STANDARD.version) == STANDARD


def test_records_without_a_definition_of_unknown_ladders() -> None:
    record = store.LadderRecord(ladder_version="custom@0123456789", status="PROVISIONING")
    with pytest.raises(ToolError) as raised:
        record.definition()
    assert raised.value.code == "NOT_FOUND"
