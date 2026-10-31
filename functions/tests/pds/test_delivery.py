"""Delivery from recorded BEACON_FIRED shapes (P0-11), and the release sweeper, against DynamoDB Local (M2-03)."""

import base64
import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from fast_channels.core.pds.book import Line
from fast_channels.core.pds.delivery import Credit, State, fill_key, home_shard, impression_key, reservation_key
from fast_channels.functions import aws
from fast_channels.functions.pds import delivery, sweeper
from fast_channels.functions.pds.reservations import Hold, reserve, set_budgets
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from dynamodb_local import Tables

FIXTURES = Path(__file__).parent / "fixtures" / "beacon_fired.jsonl"
CDN = "https://d111111abcdef8.cloudfront.net"
S1 = "0a1e6454-90e6-423a-80c6-5e02eff31cdc"
S2 = "7c3d2b1a-0f9e-4d8c-b7a6-5e4d3c2b1a09"
RESERVED_AT = datetime(2026, 10, 29, 14, 59, 50, tzinfo=UTC)
LINES = (
    Line(
        id="direct-1",
        advertiser="Direct Advertiser 1",
        adomain="direct1.example",
        creatives=("direct1",),
        cpm=Decimal(8),
        budget=Decimal(100),
        tier0=True,
    ),
    Line(
        id="direct-2",
        advertiser="Direct Advertiser 2",
        adomain="direct2.example",
        creatives=("direct2",),
        cpm=Decimal(6),
        budget=Decimal(1),
    ),
)


@dataclass
class Context:
    function_name: str = "fastm-pds-delivery"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-pds-delivery"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-pds-delivery"
    log_stream_name: str = "stream"
    left_ms: int = 60_000

    def get_remaining_time_in_millis(self) -> int:
        return self.left_ms


def subscription(events: list[dict[str, Any]]) -> dict[str, Any]:
    """A CloudWatch Logs subscription delivery of ``events`` (gzip, base64), as Lambda receives it."""
    payload = {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "/aws/vendedlogs/mediatailor/fastm",
        "logStream": "AdDecisionServerInteractions",
        "subscriptionFilters": ["fastm-pds-delivery"],
        "logEvents": [
            {"id": str(n), "timestamp": 1793286001000 + n, "message": json.dumps(event)}
            for n, event in enumerate(events)
        ],
    }
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}


def recorded() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURES.read_text().splitlines()]


@pytest.fixture
def stage(dynamodb: Tables, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tables]:
    monkeypatch.setenv("PDS_TABLE", dynamodb.pds)
    monkeypatch.setenv("LEDGER_TABLE", dynamodb.ledger)
    monkeypatch.setenv("CONTENT_BASE_URL", CDN)
    monkeypatch.setattr(aws, "client", lambda service: dynamodb.client)
    settings.cache_clear()
    delivery.host.cache_clear()
    set_budgets(dynamodb.client, table=dynamodb.pds, ledger_table=dynamodb.ledger, lines=LINES, now=RESERVED_AT)
    for sid, evid, line in ((S1, "1234", LINES[0]), (S2, "77", LINES[1])):
        decision = f"{sid}.{evid}"
        reserve(
            dynamodb.client,
            table=dynamodb.pds,
            ledger_table=dynamodb.ledger,
            decision=decision,
            sid=sid,
            holds=[Hold(line=line, crids=line.creatives, shard=home_shard(decision))],
            now=RESERVED_AT,
        )
    yield dynamodb
    settings.cache_clear()
    delivery.host.cache_clear()


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [vars(r) for r in caplog.records if getattr(r, "record", None) == "pds.delivery"]


def test_recorded_beacons_deliver_the_reservations_with_the_dedupe_keys(
    stage: Tables, caplog: pytest.LogCaptureFixture
) -> None:
    totals = delivery.handler(subscription(recorded()), Context())

    assert totals == {"beacons": 5, "impressions": 4, "fills": 2, "delivered": 2, "late": 0, "ignored": 3}
    logged = [
        (r["decision_id"].rsplit(".", 1)[1], r["crid"], r["impression"], r["fill"], r["credit"], r["line"])
        for r in records(caplog)
    ]
    assert logged == [
        ("1234", "direct1", True, True, "delivered", "direct-1"),
        ("1234", "house1", True, False, "unreserved", None),
        ("1234", "sim2", True, False, "unreserved", None),
        ("1234", "direct1", False, False, "repeat", "direct-1"),
        ("77", "direct2", True, True, "delivered", "direct-2"),
    ]
    first = records(caplog)[0]
    assert (first["pc"], first["status"], first["enr"], first["amount"]) == ("fastm-demo-p-a", 200, 0, 0.008)
    assert records(caplog)[2]["enr"] == 1
    for sid, evid, crid in ((S1, "1234", "direct1"), (S2, "77", "direct2")):
        item = stage.item(reservation_key(f"{sid}.{evid}", crid))
        assert item is not None and item["state"]["S"] == State.DELIVERED
    assert stage.item(impression_key(f"{S1}.1234", "house1")) is not None
    assert stage.item(fill_key(f"{S1}.1234")) is not None
    assert stage.item(impression_key(f"{S1}.1234", "house-ad-1-aa4d97-standard_843ed1d05a-20d4b4")) is None


def test_a_redelivered_batch_counts_nothing_twice(stage: Tables, caplog: pytest.LogCaptureFixture) -> None:
    delivery.handler(subscription(recorded()), Context())
    caplog.clear()

    again = delivery.handler(subscription(recorded()), Context())

    assert (again["impressions"], again["fills"], again["delivered"]) == (0, 0, 0)
    assert {r["credit"] for r in records(caplog)} == {Credit.REPEAT.value, Credit.UNRESERVED.value}
    deliveries = [e for e in stage.ledger_entries() if e["action"]["S"] == "money.deliver"]
    assert len(deliveries) == 2


def test_a_firing_of_ours_that_cannot_be_read_is_logged_and_skipped(
    stage: Tables, caplog: pytest.LogCaptureFixture
) -> None:
    broken = recorded()[0]
    broken["beaconInfo"]["beaconUri"] = f"{CDN}/ads/b.gif?d={S1}.1234&e=impression&enr=0"
    other = recorded()[0] | {"sessionId": S2}

    totals = delivery.handler(subscription([broken, other, {"eventType": "FILLED_AVAIL"}]), Context())

    assert (totals["beacons"], totals["ignored"]) == (0, 3)
    reasons = [getattr(r, "reason", None) for r in caplog.records if r.getMessage() == "tracker firing ignored"]
    assert reasons == ["malformed", "session_mismatch"]


def test_a_non_json_message_is_ignored(stage: Tables) -> None:
    event = subscription([])
    payload = json.loads(gzip.decompress(base64.b64decode(event["awslogs"]["data"])))
    payload["logEvents"] = [
        {"id": "1", "timestamp": 1, "message": "not json"},
        {"id": "2", "timestamp": 2, "message": "[1]"},
    ]
    event = {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(payload).encode())).decode()}}

    assert delivery.handler(event, Context())["ignored"] == 2


def test_the_sweeper_releases_what_saw_no_impression_in_two_hours(stage: Tables) -> None:
    delivery.handler(subscription(recorded()[:1]), Context())  # direct1 of S1 is delivered; direct2 of S2 is not

    early = sweeper.sweep(
        stage.pds, stage.ledger, now=RESERVED_AT + timedelta(hours=1, minutes=59), time_left=lambda: 60_000
    )
    late = sweeper.sweep(
        stage.pds, stage.ledger, now=RESERVED_AT + timedelta(hours=2, minutes=1), time_left=lambda: 60_000
    )
    after = sweeper.sweep(stage.pds, stage.ledger, now=RESERVED_AT + timedelta(hours=3), time_left=lambda: 60_000)

    assert (early, late, after) == (
        {"released": 0, "skipped": 0},
        {"released": 1, "skipped": 0},
        {"released": 0, "skipped": 0},
    )
    item = stage.item(reservation_key(f"{S2}.77", "direct2"))
    assert item is not None and item["state"]["S"] == State.RELEASED
    budget = [stage.item({"pk": "BUDGET#direct-2", "sk": f"S#{n}"}) for n in range(4)]
    assert sum(Decimal(b["remaining"]["N"]) for b in budget if b) == Decimal(1)


def test_the_sweeper_stops_before_its_timeout(stage: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    totals = sweeper.handler({}, Context(left_ms=5_000))

    assert totals == {"released": 0, "skipped": 0}
    monkeypatch.setattr(sweeper, "RELEASE_AFTER", timedelta(0))
    assert sweeper.sweep(stage.pds, stage.ledger, now=datetime.now(UTC), time_left=lambda: 5_000)["released"] == 0
    assert sweeper.sweep(stage.pds, stage.ledger, now=datetime.now(UTC), time_left=lambda: 60_000)["released"] == 2


def test_the_sweeper_pages_through_the_index(stage: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweeper, "PAGE", 1)
    due = list(sweeper.due(stage.client, stage.pds, before=RESERVED_AT + timedelta(seconds=1)))

    assert sorted(due) == sorted([(f"{S1}.1234", "direct1"), (f"{S2}.77", "direct2")])


def test_a_failed_count_fails_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(aws, "client", lambda service: client)
    with Stubber(client) as stubber:
        stubber.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException")
        with pytest.raises(ClientError):
            delivery._first("t", impression_key(f"{S1}.1", "c"), RESERVED_AT)


@pytest.mark.parametrize(
    ("message", "raised"),
    [("Cannot read from backfilling global secondary index: pending", False), ("Some other problem", True)],
)
def test_the_sweeper_waits_for_a_backfilling_index(monkeypatch: pytest.MonkeyPatch, message: str, raised: bool) -> None:
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(aws, "client", lambda service: client)
    with Stubber(client) as stubber:
        stubber.add_client_error("query", service_error_code="ValidationException", service_message=message)
        if raised:
            with pytest.raises(ClientError):
                sweeper.sweep("t", "l", now=RESERVED_AT, time_left=lambda: 60_000)
        else:
            assert sweeper.sweep("t", "l", now=RESERVED_AT, time_left=lambda: 60_000) == {"released": 0, "skipped": 0}
