"""Delivery from recorded BEACON_FIRED shapes (P0-11), and the release sweeper, against DynamoDB Local (M2-03)."""

import base64
import gzip
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from fast_channels.core.pds.book import Line
from fast_channels.core.pds.delivery import (
    CDN_LOG_EVERY,
    Credit,
    Source,
    State,
    fill_key,
    home_shard,
    impression_key,
    reservation_key,
)
from fast_channels.core.pds.pod import tracker_url
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


@dataclass
class LogFile:
    """One CloudFront log file: when it arrived, and its rows of our tracker that the select join keeps."""

    arrived: datetime
    rows: list[dict[str, str]]


@dataclass
class Athena:
    """An Athena double for the CDN-log query: it returns the rows of the files that arrived in the query's window
    and whose requests are in the hours it reads, as Athena does with ``$file_modified_time`` and the partitions."""

    files: list[LogFile] = field(default_factory=list)
    state: str = "SUCCEEDED"
    queries: list[str] = field(default_factory=list)

    def start_query_execution(self, *, QueryString: str, WorkGroup: str) -> dict[str, str]:  # noqa: N803
        assert WorkGroup == "fastm-lake"
        self.queries.append(QueryString)
        return {"QueryExecutionId": str(len(self.queries))}

    def get_query_execution(self, *, QueryExecutionId: str) -> dict[str, Any]:  # noqa: N803
        status = {"State": self.state, "StateChangeReason": "boom" if self.state == "FAILED" else None}
        return {"QueryExecution": {"Status": status, "Statistics": {"DataScannedInBytes": 291}}}

    def get_paginator(self, name: str) -> Any:
        assert name == "get_query_results"
        athena = self

        class Pages:
            def paginate(self, *, QueryExecutionId: str) -> Iterator[dict[str, Any]]:  # noqa: N803
                rows = athena.rows(athena.queries[int(QueryExecutionId) - 1])
                names = ("timestamp_ms", "cs_uri_stem", "cs_uri_query", "sc_status", "pc")
                header = [{"VarCharValue": name} for name in names]
                data = [header] + [[{"VarCharValue": row[name]} for name in row] for row in rows]
                for n in range(0, len(data), 2):  # two rows a page
                    yield {"ResultSet": {"Rows": [{"Data": d} for d in data[n : n + 2]]}}

        return Pages()

    def rows(self, query: str) -> list[dict[str, str]]:
        start, end = (datetime.fromisoformat(t) for t in re.findall(r"from_iso8601_timestamp\('([^']+)'\)", query))
        hours = set(re.findall(r"year = '(\d+)' AND month = '(\d+)' AND day = '(\d+)' AND hour = '(\d+)'", query))
        return [
            row
            for file in self.files
            if start <= file.arrived < end
            for row in file.rows
            if tuple(f"{requested(row):%Y %m %d %H}".split()) in hours
        ]


def requested(row: dict[str, str]) -> datetime:
    return datetime.fromtimestamp(int(row["timestamp_ms"]) / 1000, UTC)


def cdn_row(decision: str, crid: str, at: datetime) -> dict[str, str]:
    """Our tracker's request as the CDN logged it: its path (with the identity module the logs have no query)."""
    stem = tracker_url(f"{CDN}/ads/b", decision, crid).removeprefix(CDN)
    return {
        "timestamp_ms": str(int(at.timestamp() * 1000)),
        "cs_uri_stem": stem,
        "cs_uri_query": "-",
        "sc_status": "200",
        "pc": "fastm-demo",
    }


def cdn_run(scheduled: datetime) -> dict[str, str]:
    """The schedule's event for its run at ``scheduled``."""
    return {"source": "cdn_log", "scheduled": scheduled.isoformat().replace("+00:00", "Z")}


@pytest.fixture
def athena() -> Athena:
    return Athena()


@pytest.fixture
def stage(dynamodb: Tables, athena: Athena, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tables]:
    monkeypatch.setenv("PDS_TABLE", dynamodb.pds)
    monkeypatch.setenv("LEDGER_TABLE", dynamodb.ledger)
    monkeypatch.setenv("CONTENT_BASE_URL", CDN)
    monkeypatch.setenv("LAKE_WORKGROUP", "fastm-lake")
    monkeypatch.setenv("CDN_LOG_TABLE", "fastm_pds.cdn_access")
    monkeypatch.setenv("SELECT_TABLE", "fastm_lake.pds_select")
    monkeypatch.setattr(aws, "client", lambda service: athena if service == "athena" else dynamodb.client)
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


# The CDN-log runs (W-2 1.32).

FIRED = RESERVED_AT + timedelta(seconds=11)
"""When MediaTailor fired the trackers of the CDN-log tests (the reservations' breaks)."""
ON_TIME = FIRED + timedelta(minutes=4)
"""When CloudFront delivered their log file: about 5 minutes after the requests (seen live)."""


def run_at(arrived: datetime) -> datetime:
    """The scheduled time of the run whose window holds ``arrived`` (runs at :00:13, :05:13, ...)."""
    first = arrived.replace(minute=0, second=13, microsecond=0) - timedelta(hours=1)
    return next(
        first + n * CDN_LOG_EVERY
        for n in range(48)
        if first + n * CDN_LOG_EVERY - timedelta(minutes=15)
        <= arrived
        < first + n * CDN_LOG_EVERY - timedelta(minutes=10)
    )


def emitted(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]


def test_a_cdn_log_run_counts_once_what_the_ads_log_left_out(
    stage: Tables, athena: Athena, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    delivery.handler(subscription(recorded()[:1]), Context())  # the ADS log has direct1 of S1, not direct2 of S2
    caplog.clear()
    capsys.readouterr()
    athena.files.append(
        LogFile(ON_TIME, [cdn_row(f"{S1}.1234", "direct1", FIRED), cdn_row(f"{S2}.77", "direct2", FIRED)])
    )

    totals = delivery.handler(cdn_run(run_at(ON_TIME)), Context())

    assert totals == {"beacons": 2, "impressions": 1, "fills": 1, "delivered": 1, "late": 0, "ignored": 0}
    [metrics] = emitted(capsys)
    assert (metrics["CdnLogBeacons"], metrics["CdnLogOnlyImpressions"], metrics["Impressions"]) == ([2.0], [1.0], [1.0])
    logged = [(r["crid"], r["impression"], r["credit"], r["source"], r["pc"]) for r in records(caplog)]
    assert logged == [
        ("direct1", False, "repeat", "cdn_log", "fastm-demo"),
        ("direct2", True, "delivered", "cdn_log", "fastm-demo"),
    ]
    item = stage.item(reservation_key(f"{S2}.77", "direct2"))
    assert item is not None and item["state"]["S"] == State.DELIVERED
    imp = stage.item(impression_key(f"{S2}.77", "direct2"))
    assert imp is not None and imp["src"]["S"] == Source.CDN_LOG
    first = stage.item(impression_key(f"{S1}.1234", "direct1"))
    assert first is not None and first["src"]["S"] == Source.ADS_LOG

    late_beacon = recorded()[-1]  # the ADS log's direct2 of S2 arrives after all: nothing more
    again = delivery.handler(subscription([late_beacon]), Context())
    assert (again["beacons"], again["impressions"], again["delivered"]) == (1, 0, 0)
    deliveries = [e for e in stage.ledger_entries() if e["action"]["S"] == "money.deliver"]
    assert len(deliveries) == 2


def test_a_log_file_forty_minutes_late_is_counted_once_by_the_run_its_arrival_falls_in(
    stage: Tables, athena: Athena, caplog: pytest.LogCaptureFixture
) -> None:
    late = ON_TIME + timedelta(minutes=40)
    athena.files += [LogFile(ON_TIME, []), LogFile(late, [cdn_row(f"{S2}.77", "direct2", FIRED)])]
    runs = [run_at(ON_TIME) + n * CDN_LOG_EVERY for n in range(24)]  # two hours of runs

    counted = [(scheduled, delivery.handler(cdn_run(scheduled), Context())["impressions"]) for scheduled in runs]

    assert [(scheduled, n) for scheduled, n in counted if n] == [(run_at(late), 1)]
    assert [r["impression"] for r in records(caplog)] == [True]
    redriven = delivery.handler(cdn_run(run_at(late)), Context())
    assert (redriven["beacons"], redriven["impressions"], redriven["delivered"]) == (1, 0, 0)


def test_a_failed_query_fails_the_run(stage: Tables, athena: Athena) -> None:
    athena.state = "FAILED"

    with pytest.raises(RuntimeError, match="FAILED: boom"):
        delivery.handler(cdn_run(run_at(ON_TIME)), Context())


def test_a_cdn_log_row_that_is_not_ours_is_skipped(
    stage: Tables, athena: Athena, caplog: pytest.LogCaptureFixture
) -> None:
    broken = cdn_row(f"{S1}.1234", "direct1", FIRED) | {"cs_uri_stem": f"/ads/b/{S1}.1234/direct1/2.gif"}
    athena.files.append(LogFile(ON_TIME, [broken]))

    totals = delivery.handler(cdn_run(run_at(ON_TIME)), Context())

    assert (totals["beacons"], totals["ignored"]) == (0, 1)
    assert [getattr(r, "reason", None) for r in caplog.records if r.getMessage() == "tracker firing ignored"] == [
        "malformed"
    ]
