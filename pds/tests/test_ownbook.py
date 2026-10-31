"""``POST /ownbook/openrtb``: OpenRTB in, the own book's bids out."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.pds.book import Book, Creative, Line
from fast_channels.core.pds.snapshot import EMPTY, Snapshot, SnapshotBase, build
from fast_channels.functions.pds.store import snapshot_store
from fast_channels.functions.settings import settings
from fast_channels.pds import ownbook, state

if TYPE_CHECKING:
    from fast_channels.pds.http import Answer

CDN = "https://d111111abcdef8.cloudfront.net"
LINE = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal(8),
    budget=Decimal(100),
    tier0=True,
)
OWN = Book(
    ladder_version="standard@843ed1d05a",
    lines=(LINE,),
    creatives=(Creative(crid="direct1", creative_id="demo-direct1", hls_uri=f"{CDN}/d1.m3u8", duration_ms=30030),),
    house=(Creative(crid="house1", creative_id="house-ad-1", hls_uri=f"{CDN}/h1.m3u8", duration_ms=30030),),
)
SNAPSHOT = build(SnapshotBase(lines=(LINE,)), own=OWN, now=datetime(2026, 10, 29, tzinfo=UTC))


class Fake:
    def __init__(self, snapshot: Snapshot, exhausted: frozenset[str] = frozenset()) -> None:
        self.snapshot, self.spent = snapshot, exhausted

    def get(self) -> Snapshot:
        return self.snapshot

    def exhausted(self, own: Book) -> frozenset[str]:
        return self.spent


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    state = Fake(SNAPSHOT)
    monkeypatch.setattr(ownbook, "store", lambda: state)
    monkeypatch.setattr(ownbook, "budgets", lambda: state)
    return state


def openrtb(slots: int = 2, rqddurs: list[int] | None = None) -> str:
    return json.dumps(
        {
            "id": "auction-7",
            "imp": [
                {"id": f"s{n}", "video": {"rqddurs": rqddurs or [30], "poddur": 30 * slots}, "ssai": 3}
                for n in range(1, slots + 1)
            ],
            "app": {"publisher": {"id": "fastm"}},
            "regs": {"coppa": 0},
        }
    )


def ask(body: str) -> Answer:
    return ownbook.answer(body.encode(), time.perf_counter())


def logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [vars(r) for r in caplog.records if r.getMessage() == "own book"]


@pytest.mark.usefixtures("fake")
def test_each_slot_gets_the_house_ads_and_the_lines(caplog: pytest.LogCaptureFixture) -> None:
    response = ask(openrtb())

    assert response.status == 200
    answer = json.loads(response.body)
    [seat] = answer["seatbid"]
    assert (answer["id"], seat["seat"]) == ("auction-7", "ownbook")
    assert [(b["impid"], b["crid"], b["price"], b.get("dealid")) for b in seat["bid"]] == [
        ("s1", "house1", 0.01, None),
        ("s1", "direct1", 8.0, "fast-direct-1"),
        ("s2", "house1", 0.01, None),
        ("s2", "direct1", 8.0, "fast-direct-1"),
    ]
    [line] = logged(caplog)
    assert (line["imps"], line["durations"], line["house"], line["lines"], line["bids"]) == (
        2,
        [30],
        2,
        ["direct-1"],
        4,
    )
    assert line["snapshot"] == SNAPSHOT.version


def test_a_line_without_budget_is_not_bid(fake: Fake) -> None:
    fake.spent = frozenset({"direct-1"})

    answer = json.loads(ask(openrtb(1)).body)

    assert [b["crid"] for b in answer["seatbid"][0]["bid"]] == ["house1"]


def test_no_bid_is_a_204_and_so_is_a_sidecar_without_a_snapshot(fake: Fake) -> None:
    assert ask(openrtb(rqddurs=[15])).status == 204, "nothing fits a 15 s slot"
    fake.snapshot = EMPTY

    response = ask(openrtb())

    assert (response.status, response.body) == (204, "")


@pytest.mark.usefixtures("fake")
@pytest.mark.parametrize("body", ["", "not json", json.dumps({"imp": []}), json.dumps({"id": "a", "imp": [{}]})])
def test_a_request_that_is_not_openrtb_is_a_400(body: str) -> None:
    response = ask(body)

    assert (response.status, json.loads(response.body)) == (400, {"error": "invalid request"})


def test_a_failure_is_a_500(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(ownbook, "response", broken)

    response = ask(openrtb())

    assert (response.status, json.loads(response.body)) == (500, {"error": "internal"})


def test_the_stores_read_the_configured_bucket_and_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The own book and ``/select`` share one budget view, on the reservations' client."""
    monkeypatch.setenv("PDS_CONFIG_BUCKET", "fastm-pds-config")
    monkeypatch.setenv("PDS_TABLE", "fastm-pds")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    settings.cache_clear()
    for cached in (snapshot_store, state.budgets, state.dynamodb):
        cached.cache_clear()
    try:
        assert ownbook.store()._bucket == "fastm-pds-config"
        budgets = ownbook.budgets()
        assert budgets._table == "fastm-pds"
        assert budgets._lines() == ()
        assert budgets.client is state.dynamodb()
    finally:
        for cached in (snapshot_store, state.budgets, state.dynamodb):
            cached.cache_clear()
        settings.cache_clear()
