"""``POST /select`` behind the internal load balancer: the pod, its line ads reserved, one decision record (M2-04)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from fast_channels.core.pds.book import Book, Creative, Line
from fast_channels.core.pds.models import SelectResponse
from fast_channels.core.pds.snapshot import Snapshot, SnapshotBase, build
from fast_channels.functions.pds import select
from fast_channels.functions.pds.budget import Budgets
from fast_channels.functions.pds.reservations import Refusal, Reservation, set_budgets
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import Tables

FIXTURES = Path(__file__).parents[3] / "contracts" / "fixtures" / "pds" / "select"
CDN = "https://d111111abcdef8.cloudfront.net"
NOW = datetime(2026, 10, 29, 16, tzinfo=UTC)
DIRECT1 = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal(8),
    budget=Decimal(100),
    tier0=True,
)


def snapshot_of(*lines: Line, breaks: tuple[int, ...] = (30030, 60060, 90090, 120120)) -> Snapshot:
    creatives = tuple(
        Creative(crid=crid, creative_id=f"demo-{crid}", hls_uri=f"{CDN}/{crid}.m3u8", duration_ms=30030)
        for line in lines
        for crid in line.creatives
    )
    own = Book(lines=lines, creatives=creatives)
    return build(SnapshotBase(lines=lines, breaks_ms=breaks), own=own, now=NOW)


@dataclass
class Context:
    function_name: str = "fastm-pds-select"
    function_version: str = "4"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-pds-select:live"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-pds-select"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 3000


class Store:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def get(self) -> Snapshot:
        return self.snapshot

    @property
    def current(self) -> Snapshot:
        return self.snapshot


class View:
    """Every line has budget on shard 0 unless told otherwise."""

    def __init__(self, empty: frozenset[str] = frozenset()) -> None:
        self.empty = empty

    def shard(self, line: Line, decision: str, amount: Decimal) -> int | None:
        return None if line.id in self.empty else 0


def request(name: str = "demo-full-120s.json", **fields: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())["request"]
    return data | fields


def alb(body: str | None, *, path: str = "/select") -> dict[str, Any]:
    return {
        "requestContext": {"elb": {"targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/pds/3"}},
        "httpMethod": "POST",
        "path": path,
        "queryStringParameters": {},
        "headers": {"content-type": "application/json", "host": "pds.fastm.fast.internal"},
        "body": body,
        "isBase64Encoded": False,
    }


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [vars(r) for r in caplog.records if getattr(r, "record", None) == "pds.select"]


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CONTENT_BASE_URL", f"{CDN}/")
    monkeypatch.setenv("PDS_TABLE", "fastm-pds")
    monkeypatch.setenv("LEDGER_TABLE", "fastm-ledger")
    monkeypatch.setenv("PDS_CONFIG_BUCKET", "fastm-pds-config")
    settings.cache_clear()
    select.tracker_base.cache_clear()
    yield
    settings.cache_clear()
    select.tracker_base.cache_clear()


@pytest.fixture
def stubbed(environment: None, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The snapshot, an open budget view and a reservation step that records what it was asked."""
    asked: list[dict[str, Any]] = []
    store, view = Store(snapshot_of(DIRECT1)), View()
    monkeypatch.setattr(select, "store", lambda: store)
    monkeypatch.setattr(select, "budgets", lambda: view)

    def fake(client: Any, **kwargs: Any) -> Reservation:
        asked.append(kwargs)
        return Reservation(held=tuple(c for hold in kwargs["holds"] for c in hold.crids))

    monkeypatch.setattr(select, "reserve", fake)
    monkeypatch.setattr(select, "dynamodb", lambda: None)
    return asked


def test_the_pod_with_its_line_ad_reserved_and_our_trackers(
    stubbed: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    response = select.handler(alb(json.dumps(request())), Context())

    assert response["statusCode"] == 200
    answer = SelectResponse.model_validate_json(response["body"])
    assert [ad.ref for ad in answer.pod] == ["ownbook-s1-direct1", "amt-s2-sim2", "amt-s3-sim3", "ownbook-s4-house2"]
    decision = request()["decision_id"]
    assert answer.trackers["ownbook-s1-direct1"] == [f"{CDN}/ads/b.gif?d={decision}&c=direct1&e=impression&enr=0"]
    [asked] = stubbed
    assert (asked["decision"], asked["sid"], asked["table"], asked["ledger_table"]) == (
        decision,
        request()["sid"],
        "fastm-pds",
        "fastm-ledger",
    )
    assert [(h.line.id, h.crids, h.shard) for h in asked["holds"]] == [("direct-1", ("direct1",), 0)]
    [record] = records(caplog)
    assert (record["reserved"], record["reason"], record["decision_id"]) == (["direct1"], "filled", decision)
    assert select.logger.get_correlation_id() == decision


def test_the_contract_examples_are_answered_as_they_say(
    stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTENT_BASE_URL", "https://cdn.example.com")
    settings.cache_clear()
    select.tracker_base.cache_clear()
    for path in sorted(FIXTURES.glob("*.json")):
        case = json.loads(path.read_text())
        deals = case["context"].get("pod", {}).get("tier0_deals", [])
        lines = (DIRECT1,) if "fast-direct-1" in deals else ()
        monkeypatch.setattr(select, "store", lambda lines=lines: Store(snapshot_of(*lines)))
        response = select.handler(alb(json.dumps(case["request"])), Context())
        assert json.loads(response["body"]) == case["response"], path.name


def test_a_line_without_budget_is_left_out(stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(select, "budgets", lambda: View(empty=frozenset({"direct-1"})))

    answer = SelectResponse.model_validate_json(select.handler(alb(json.dumps(request())), Context())["body"])

    assert next(ad.ref for ad in answer.pod) == "amt-s1-sim1"
    assert stubbed == [], "no shard has its price: nothing to reserve"


@pytest.mark.parametrize(
    "failure",
    [ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems"), Refusal.BUDGET],
)
def test_a_refused_or_failed_reservation_leaves_the_line_out(
    stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: Any
) -> None:
    def broken(client: Any, **kwargs: Any) -> Reservation:
        if isinstance(failure, Exception):
            raise failure
        return Reservation(refused={c: failure for hold in kwargs["holds"] for c in hold.crids})

    monkeypatch.setattr(select, "reserve", broken)

    answer = SelectResponse.model_validate_json(select.handler(alb(json.dumps(request())), Context())["body"])

    assert "ownbook-s1-direct1" not in [ad.ref for ad in answer.pod]
    [record] = records(caplog)
    assert record["reserved"] == []
    [direct1] = [bid for bid in record["bids"] if bid["ref"] == "ownbook-s1-direct1"]
    assert direct1["outcome"] == "unreserved"


@pytest.mark.usefixtures("stubbed")
def test_a_prefetched_break_takes_the_schedules_duration() -> None:
    body = request("demo-full-60s.json", dur_ms=60000)

    answer = SelectResponse.model_validate_json(select.handler(alb(json.dumps(body)), Context())["body"])

    assert len(answer.pod) == 2, "two 30.03 s ads fit the 60.06 s break of record"


@pytest.mark.usefixtures("stubbed")
@pytest.mark.parametrize("body", ["", "not json", json.dumps(request(v=2)), json.dumps(request(decision_id="x.1"))])
def test_a_request_outside_the_contract_is_refused(body: str) -> None:
    response = select.handler(alb(body), Context())

    assert (response["statusCode"], json.loads(response["body"])) == (400, {"error": "invalid request"})


def test_a_failure_is_a_500(stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(select, "select_response", broken)

    response = select.handler(alb(json.dumps(request())), Context())

    assert (response["statusCode"], json.loads(response["body"])) == (500, {"error": "internal"})


@pytest.mark.usefixtures("environment")
def test_the_helpers_read_the_configuration() -> None:
    select.budgets.cache_clear()
    select.dynamodb.cache_clear()
    try:
        assert select.tracker_base() == f"{CDN}/ads/b.gif"
        assert select.budgets()._table == "fastm-pds"
        assert select.dynamodb().meta.config.read_timeout == 0.1
    finally:
        select.budgets.cache_clear()
        select.dynamodb.cache_clear()


def test_a_one_dollar_line_stops_serving_once_its_budget_is_reserved(
    dynamodb: Tables, environment: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Against DynamoDB Local: $1 at $6 CPM reserves 164 impressions (41 per $0.25 shard), then the line is out."""
    small = Line(
        id="direct-1",
        advertiser="Direct Advertiser 1",
        adomain="direct1.example",
        creatives=("direct1",),
        cpm=Decimal(6),
        budget=Decimal(1),
        tier0=True,
    )
    monkeypatch.setenv("PDS_TABLE", dynamodb.pds)
    monkeypatch.setenv("LEDGER_TABLE", dynamodb.ledger)
    settings.cache_clear()
    set_budgets(dynamodb.client, table=dynamodb.pds, ledger_table=dynamodb.ledger, lines=[small], now=NOW)
    snapshot = snapshot_of(small)
    view = Budgets(dynamodb.pds, lines=lambda: (small,), client=dynamodb.client, spawn=lambda target: target())
    monkeypatch.setattr(select, "store", lambda: Store(snapshot))
    monkeypatch.setattr(select, "budgets", lambda: view)
    monkeypatch.setattr(select, "dynamodb", lambda: dynamodb.client)
    served: list[bool] = []
    for evid in range(200):
        view._next = float("-inf")  # read the shards before every break: the test has no clock
        body = request(evid=str(evid), decision_id=f"{request()['sid']}.{evid}")
        answer = SelectResponse.model_validate_json(select.handler(alb(json.dumps(body)), Context())["body"])
        served.append(any(ad.ref == "ownbook-s1-direct1" for ad in answer.pod))

    assert served.count(True) == 164
    assert served[-10:] == [False] * 10, "the line stopped serving"
    unreserved = [r for r in records(caplog) if not r["reserved"]]
    assert len(unreserved) == 36


def test_no_reservation_starts_late(stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(select, "RESERVE_BEFORE_S", -1.0)

    answer = SelectResponse.model_validate_json(select.handler(alb(json.dumps(request())), Context())["body"])

    assert stubbed == []
    assert "ownbook-s1-direct1" not in [ad.ref for ad in answer.pod]


def test_a_bad_tracker_base_fails_before_anything_is_reserved(
    stubbed: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTENT_BASE_URL", "http://insecure.example")
    settings.cache_clear()
    select.tracker_base.cache_clear()

    response = select.handler(alb(json.dumps(request())), Context())

    assert response["statusCode"] == 500
    assert stubbed == [], "no reservation for an answer that cannot be made"
