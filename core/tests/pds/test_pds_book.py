"""The own book (M2-03): the kit's house ads and the direct lines, bid per imp."""

from decimal import Decimal
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from pydantic import ValidationError

from fast_channels.core.demo_demand import DEMO_CREATIVES, demo_index
from fast_channels.core.pds.book import (
    AD_SYSTEM,
    HOUSE_PRICE,
    BidRequest,
    Book,
    Creative,
    Imp,
    Line,
    bids,
    book,
    deal_id,
    house_creatives,
    response,
)
from fast_channels.core.pds.models import OWN_BOOK
from fast_channels.core.specs import Creative as KitCreative
from fast_channels.core.specs import vast_pod

CDN = "https://d111111abcdef8.cloudfront.net"
LADDER = "standard@843ed1d05a"
KIT = [
    KitCreative(
        creative_id=f"house-ad-{n}-aa4d97-standard_843ed1d05a-20d4b4",
        duration_ms=30030,
        hls_uri=f"{CDN}/ads/standard_843ed1d05a/house-ad-{n}-aa4d97-standard_843ed1d05a-20d4b4/main.m3u8",
    )
    for n in (1, 2)
]
INDEX = demo_index(
    LADDER,
    {
        c.crid: (f"demo-{c.crid}-0f0f0f-standard_843ed1d05a-20d4b4", f"{CDN}/ads/x/demo-{c.crid}/main.m3u8", 30030)
        for c in DEMO_CREATIVES
    },
    width=1920,
    height=1080,
)
DIRECT1 = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal("8.00"),
    budget=Decimal(100),
    tier0=True,
)
DIRECT2 = Line(
    id="direct-2",
    advertiser="Direct Advertiser 2",
    adomain="direct2.example",
    cat=("IAB1",),
    creatives=("direct2",),
    cpm=Decimal("6.00"),
    budget=Decimal(1),
    per_session=4,
)
HOUSE = house_creatives(vast_pod(creatives=KIT, content_base_url=CDN, fill_ms=120_120))
OWN = book((DIRECT1, DIRECT2), house=HOUSE, index=INDEX)


def request(*durations: list[Any] | None) -> BidRequest:
    return BidRequest.model_validate(
        {
            "id": "auction-1",
            "imp": [
                {"id": f"s{n}", "video": {} if d is None else {"rqddurs": d, "poddur": 120}, "ext": {"prebid": {}}}
                for n, d in enumerate(durations, start=1)
            ],
            "app": {"id": "fastm-app"},
        }
    )


def test_the_house_ads_are_the_kits_vast_pod_once_each() -> None:
    assert [(c.crid, c.creative_id, c.duration_ms, c.seconds) for c in HOUSE] == [
        ("house1", KIT[0].creative_id, 30030, 30),
        ("house2", KIT[1].creative_id, 30030, 30),
    ]
    assert HOUSE[0].hls_uri == KIT[0].hls_uri
    assert (HOUSE[0].width, HOUSE[0].height, HOUSE[0].advertiser) == (1920, 1080, "")


@pytest.mark.parametrize(
    "document",
    [
        "not xml",
        '<VAST xmlns="http://www.iab.com/VAST"><Ad><InLine><Creatives><Creative id="a"><Linear>'
        "<Duration>00:00:30.030</Duration></Linear></Creative></Creatives></InLine></Ad></VAST>",
    ],
    ids=["not-xml", "no-media"],
)
def test_a_kit_pod_that_is_not_a_pod_of_streaming_ads_is_refused(document: str) -> None:
    with pytest.raises(ValueError, match=r"not XML|no duration or streaming media"):
        house_creatives(document)


def test_the_book_takes_the_lines_creatives_from_the_kits_demo_index() -> None:
    assert OWN.ladder_version == LADDER
    assert [(c.crid, c.advertiser) for c in OWN.creatives] == [
        ("direct1", "Direct Advertiser 1"),
        ("direct2", "Direct Advertiser 2"),
    ]
    assert OWN.tier0_deals == frozenset({"fast-direct-1"})
    assert OWN.line_of("direct2") is DIRECT2
    assert OWN.line_of("house1") is None
    assert DIRECT1.insertion == Decimal("0.008")
    without = book((DIRECT1,), house=HOUSE, index=None)
    assert (without.ladder_version, without.creatives, without.lines) == (None, (), (DIRECT1,))


def test_a_line_whose_creative_the_kit_lacks_bids_nothing() -> None:
    partial = demo_index(
        LADDER, {"direct2": ("demo-direct2", f"{CDN}/ads/x/demo-direct2/main.m3u8", 30030)}, width=1920, height=1080
    )
    own = book((DIRECT1, DIRECT2), house=(), index=partial)
    placed = bids(request([30]), own)
    assert [b["crid"] for b in placed] == ["direct2"]


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        ((DIRECT1, DIRECT1), "line ids must be unique"),
        ((DIRECT1, DIRECT2.model_copy(update={"id": "direct-3", "creatives": ("direct1",)})), "at most one line"),
    ],
)
def test_lines_are_unique_and_a_creative_serves_one(lines: tuple[Line, ...], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Book(lines=lines)


def test_creative_ids_are_unique() -> None:
    with pytest.raises(ValidationError, match="creative ids must be unique"):
        Book(house=(HOUSE[0], HOUSE[0]))


def test_every_imp_gets_the_house_ads_and_the_lines_that_fit() -> None:
    placed = bids(request([30], [30]), OWN)

    assert [(b["impid"], b["crid"], b["price"]) for b in placed] == [
        ("s1", "house1", 0.01),
        ("s1", "house2", 0.01),
        ("s1", "direct1", 8.0),
        ("s1", "direct2", 6.0),
        ("s2", "house1", 0.01),
        ("s2", "house2", 0.01),
        ("s2", "direct1", 8.0),
        ("s2", "direct2", 6.0),
    ]
    assert len({b["id"] for b in placed}) == len(placed)
    house, _, direct1, direct2 = placed[:4]
    assert house["ext"] == {"fast": {"house": True}} and "adomain" not in house and "dealid" not in house
    assert direct1["ext"] == {"fast": {"line": "direct-1"}}
    assert (direct1["adomain"], direct1["dealid"], "cat" in direct1) == (
        ["direct1.example"],
        deal_id("direct-1"),
        False,
    )
    assert (direct2["adomain"], direct2["cat"], "dealid" in direct2) == (["direct2.example"], ["IAB1"], False)
    assert {(b["dur"], b["w"], b["h"], b["mtype"]) for b in placed} == {(30, 1920, 1080, 2)}
    assert all("burl" not in b and "nurl" not in b for b in placed), "the module adds our tracker"
    assert Decimal("0.01") == HOUSE_PRICE


def test_the_adm_is_an_inline_ad_of_the_preconditioned_creative_without_an_impression() -> None:
    [direct1] = [b for b in bids(request([30]), OWN) if b["crid"] == "direct1"]
    vast = ET.fromstring(direct1["adm"])

    assert vast.get("version") == "4.2"
    assert vast.findtext("Ad/InLine/AdSystem") == AD_SYSTEM
    assert vast.findtext("Ad/InLine/Advertiser") == "Direct Advertiser 1"
    assert vast.find("Ad/InLine/Impression") is None
    assert vast.findtext("Ad/InLine/Creatives/Creative/Linear/Duration") == "00:00:30.030"
    media = vast.find("Ad/InLine/Creatives/Creative/Linear/MediaFiles/MediaFile")
    assert media is not None and media.text == OWN.creatives[0].hls_uri
    assert media.get("type") == "application/x-mpegURL"
    [house] = [b for b in bids(request([30]), OWN) if b["crid"] == "house1"]
    assert ET.fromstring(house["adm"]).find("Ad/InLine/Advertiser") is None


def test_an_exhausted_line_is_not_bid() -> None:
    placed = bids(request([30]), OWN, exhausted={"direct-1"})
    assert [b["crid"] for b in placed] == ["house1", "house2", "direct2"]


@pytest.mark.parametrize("durations", [[15], [], None, ["30"], [True], 30], ids=str)
def test_only_slots_that_accept_the_creatives_duration_are_bid(durations: Any) -> None:
    assert bids(request(durations), OWN) == []
    assert response(request(durations), OWN) is None


def test_the_response_is_one_seat_of_the_own_book() -> None:
    answer = response(request([30]), OWN)

    assert answer is not None
    assert (answer["id"], answer["cur"]) == ("auction-1", "USD")
    [seat] = answer["seatbid"]
    assert seat["seat"] == OWN_BOOK and len(seat["bid"]) == 4


def test_the_request_is_read_leniently_but_needs_ids() -> None:
    assert Imp.model_validate({"id": "s1", "video": {"rqddurs": [30, 15, 0, -1]}}).durations == {30, 15}
    with pytest.raises(ValidationError):
        BidRequest.model_validate({"imp": []})
    with pytest.raises(ValidationError):
        BidRequest.model_validate({"id": "a", "imp": [{"id": "bad id"}]})


def test_a_creatives_seconds_round_half_up() -> None:
    def seconds(ms: int) -> int:
        return Creative(crid="c", creative_id="c", hls_uri=f"{CDN}/c.m3u8", duration_ms=ms).seconds

    assert (seconds(30030), seconds(29500), seconds(29499), seconds(15015)) == (30, 30, 29, 15)
