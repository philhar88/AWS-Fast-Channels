"""Agentic selling (M7): the products, and booked buys as own-book lines with their delivery."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fast_channels.core.pds.book import LINE_ID_PATTERN, Line
from fast_channels.core.selling import names
from fast_channels.core.selling.booking import (
    Booking,
    Buy,
    Package,
    Unserved,
    booked_lines,
    delivery,
    impressions,
    line_id,
)
from fast_channels.core.selling.products import FORMAT, product_id, products

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
BREAKS = (30030, 60060, 90090, 120120)
PRODUCTS = products(
    ("fastcii-demo",),
    genres=["Animation", "sci-fi", "animation", " "],
    breaks_ms=BREAKS,
    cpm=Decimal(40),
    publisher_domain="fast-channels.example",
)


def buy(media_buy_id: str = "buy_595f4386", *, minutes: int = 0, **changes: object) -> Buy:
    data: dict[str, object] = {
        "media_buy_id": media_buy_id,
        "status": "pending_creatives",
        "created_at": NOW + timedelta(minutes=minutes),
        "starts": NOW - timedelta(hours=1),
        "ends": NOW + timedelta(days=7),
        "advertiser": "AWS reference buyer",
        "brand": "acme-outdoor.example",
        "packages": [
            {"package_id": "pkg_1", "product_id": "fastcii-demo-midroll", "budget": "50", "cpm": "40"},
        ],
    }
    return Buy.model_validate({**data, **changes})


def test_a_product_per_monetized_channel_names_its_breaks_and_the_catalogs_genres() -> None:
    [product] = PRODUCTS

    assert (product.product_id, product.channel, product.cpm) == ("fastcii-demo-midroll", "fastcii-demo", 40)
    assert product.format_ids == (FORMAT,) and product.delivery_type == "non_guaranteed"
    assert "Its breaks last 30 s, 60 s, 90 s or 120 s, sold in whole 30 s slots" in product.description
    assert "Programs from the catalog's genres: animation, sci-fi." in product.description
    assert "not an accredited measurement" in product.description
    assert product.placements[0]["placement_id"] == "fastcii-demo-midroll"


def test_a_catalog_without_genres_says_so() -> None:
    [product] = products(["x-demo"], genres=[], breaks_ms=[30030], cpm=Decimal(1), publisher_domain="a.example")

    assert "The catalog lists no genres yet." in product.description
    assert "Its breaks last 30 s," in product.description
    assert product_id("x-demo") == product.product_id


def test_a_served_package_becomes_a_line_with_the_buys_terms() -> None:
    booking = booked_lines([buy()], products=PRODUCTS, pool=("direct2",), now=NOW)

    [line] = booking.lines
    assert line == Line(
        id="adcp-buy-595f4386-1",
        advertiser="AWS reference buyer",
        adomain="acme-outdoor.example",
        creatives=("direct2",),
        cpm=Decimal(40),
        budget=Decimal(50),
        starts=NOW - timedelta(hours=1),
        ends=NOW + timedelta(days=7),
        channels=("fastcii-demo",),
    )
    assert booking.packages == {"adcp-buy-595f4386-1": ("buy_595f4386", "pkg_1")} and not booking.unserved


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "canceled"},
        {"status": "pending_approval"},
        {"status": "completed"},
        {"paused": True},
        {"ends": NOW},
    ],
    ids=str,
)
def test_a_buy_that_is_not_served_has_no_line(changes: dict[str, object]) -> None:
    assert booked_lines([buy(**changes)], products=PRODUCTS, pool=("direct2",), now=NOW).lines == ()


@pytest.mark.parametrize(
    ("package", "changes", "reason"),
    [
        ({"product_id": "other"}, {}, Unserved.PRODUCT),
        ({"cpm": None}, {}, Unserved.PRICING),
        ({}, {"currency": "EUR"}, Unserved.PRICING),
        ({"budget": None}, {}, Unserved.BUDGET),
        ({}, {"brand": None}, Unserved.BRAND),
        ({}, {"brand": "not a domain"}, Unserved.BRAND),
    ],
)
def test_a_package_that_cannot_be_served_says_why(
    package: dict[str, object], changes: dict[str, object], reason: Unserved
) -> None:
    base = {"package_id": "pkg_1", "product_id": "fastcii-demo-midroll", "budget": "50", "cpm": "40"}
    booking = booked_lines(
        [buy(packages=[{**base, **package}], **changes)], products=PRODUCTS, pool=("direct2",), now=NOW
    )

    assert (booking.lines, booking.unserved) == ((), {"buy_595f4386/pkg_1": reason})


def test_creatives_go_to_buys_in_booking_order_and_stay_with_their_line() -> None:
    first, second, third = buy("buy_a", minutes=1), buy("buy_b", minutes=2), buy("buy_c", minutes=3)
    pool = ("direct1", "direct2")

    booking = booked_lines([third, second, first], products=PRODUCTS, pool=pool, now=NOW)
    assert [(line.id, line.creatives) for line in booking.lines] == [
        ("adcp-buy-a-1", ("direct1",)),
        ("adcp-buy-b-1", ("direct2",)),
    ]
    assert booking.unserved == {"buy_c/pkg_1": Unserved.CREATIVE}

    after = booked_lines([third, second], products=PRODUCTS, pool=pool, current=booking.lines, now=NOW)
    assert [(line.id, line.creatives) for line in after.lines] == [
        ("adcp-buy-b-1", ("direct2",)),  # kept, though buy_a's end freed the first creative
        ("adcp-buy-c-1", ("direct1",)),
    ]


def test_a_held_creative_outside_the_pool_is_given_up() -> None:
    held = booked_lines([buy()], products=PRODUCTS, pool=("direct2",), now=NOW).lines

    [line] = booked_lines([buy()], products=PRODUCTS, pool=("direct1",), current=held, now=NOW).lines
    assert line.creatives == ("direct1",)


@pytest.mark.parametrize(
    ("media_buy_id", "expected"),
    [("buy_595f4386", "adcp-buy-595f4386-2"), ("MB--x", "adcp-mb-x-2"), ("b" * 60, None)],
)
def test_line_ids_are_readable_when_they_fit(media_buy_id: str, expected: str | None) -> None:
    found = line_id(media_buy_id, 2)
    assert re.fullmatch(LINE_ID_PATTERN, found)
    if expected is not None:
        assert found == expected
    else:
        assert found.startswith("adcp-") and len(found) == len("adcp-") + 16


def test_delivery_counts_impressions_from_what_each_line_delivered() -> None:
    two = buy(
        packages=[
            {"package_id": "pkg_1", "product_id": "fastcii-demo-midroll", "budget": "50", "cpm": "40"},
            {"package_id": "pkg_2", "product_id": "fastcii-demo-midroll", "budget": "10", "cpm": "12.5"},
        ]
    )
    booking = booked_lines([two], products=PRODUCTS, pool=("direct1", "direct2"), now=NOW)

    [report] = delivery(booking, {"adcp-buy-595f4386-1": Decimal("0.12"), "adcp-buy-595f4386-2": Decimal("0.0125")})

    assert (report.media_buy_id, report.impressions, report.spend) == ("buy_595f4386", 4, Decimal("0.1325"))
    assert [(p.package_id, p.impressions) for p in report.packages] == [("pkg_1", 3), ("pkg_2", 1)]
    assert report.source == "seller server count, not MRC"


def test_a_line_not_read_delivered_nothing() -> None:
    booking = booked_lines([buy()], products=PRODUCTS, pool=("direct2",), now=NOW)

    assert [(r.impressions, r.spend) for r in delivery(booking, {})] == [(0, 0)]
    assert delivery(Booking(), {}) == []
    assert impressions(Decimal("0.039999"), booking.lines[0]) == 1


def test_the_buy_is_read_from_the_bridge_leniently() -> None:
    assert Package.model_validate({"package_id": "p", "product_id": "x", "extra": 1}).cpm is None
    with pytest.raises(ValidationError):
        Buy.model_validate({"media_buy_id": "b", "status": "active"})


def test_resource_names() -> None:
    assert names.runtime("fastcii", names.Image.BUYER) == "fastcii_buyer"
    assert names.image_repository("fastcii", names.Image.GOVERNANCE) == "fastcii-governance"
    assert names.governance_signing_alias("fastcii") == "alias/fastcii-governance-signing"
    assert names.buyer_identity("fastcii").endswith(".invalid/a2a")
