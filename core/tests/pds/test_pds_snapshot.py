from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

from fast_channels.core.pbs_config import pds_host
from fast_channels.core.pds import names
from fast_channels.core.pds.book import Book, Creative, Line
from fast_channels.core.pds.policy import Demand
from fast_channels.core.pds.snapshot import (
    EMPTY,
    HISTORY_PREFIX,
    Snapshot,
    SnapshotBase,
    build,
    demo_demand,
    digest,
    from_ip_ranges,
    history_key,
)

NOW = datetime(2026, 10, 29, 13, 4, 5, tzinfo=UTC)
RANGES = {
    "syncToken": "1793278000",
    "prefixes": [
        {"ip_prefix": "130.176.0.0/18", "service": "CLOUDFRONT_ORIGIN_FACING"},
        {"ip_prefix": "3.80.0.0/12", "service": "EC2"},
    ],
    "ipv6_prefixes": [{"ipv6_prefix": "2600:9000:1000::/36", "service": "CLOUDFRONT_ORIGIN_FACING"}],
}
BASE = SnapshotBase(demand=demo_demand("fastm"), shed=frozenset({"other"}), floor_cpm=Decimal("0.5"))
NETWORKS, SYNC = from_ip_ranges(RANGES)


def snapshot_at(now: datetime, base: SnapshotBase = BASE, sync: str | None = SYNC) -> Snapshot:
    return build(base, origin_facing=NETWORKS, ip_ranges=sync, now=now)


def test_a_snapshot_carries_the_configuration_and_the_origin_facing_networks() -> None:
    snapshot = snapshot_at(NOW)

    assert snapshot.version.startswith("20261029T130405Z-")
    assert snapshot.origin_facing == ("130.176.0.0/18", "2600:9000:1000::/36")
    assert snapshot.networks == (ip_network("130.176.0.0/18"), ip_network("2600:9000:1000::/36"))
    assert snapshot.ip_ranges == "1793278000"
    assert snapshot.demand == {"amt": {"placementId": "fastm-demo"}}
    assert snapshot.allow_list == Demand({"amt": {"placementId": "fastm-demo"}}, frozenset({"other"}))
    assert snapshot.floor_cpm == Decimal("0.5")
    assert history_key(snapshot) == f"{HISTORY_PREFIX}{snapshot.version}.json"


def test_the_digest_follows_the_content_not_the_time() -> None:
    first, later = snapshot_at(NOW), snapshot_at(datetime(2026, 10, 30, tzinfo=UTC), sync="1793279999")
    changed = snapshot_at(NOW, BASE.model_copy(update={"floor_cpm": Decimal(1)}))

    assert first.version != later.version
    assert digest(first) == digest(later) != digest(changed), "a new ip-ranges.json alone changes nothing"


def test_shed_bidders_serialize_sorted() -> None:
    many = SnapshotBase(shed=frozenset({"zeta", "alpha", "mid"}))

    assert many.model_dump(mode="json")["shed"] == ["alpha", "mid", "zeta"]
    assert '"shed":["alpha","mid","zeta"]' in many.model_dump_json()


def test_without_ip_ranges_there_is_no_network() -> None:
    snapshot = build(BASE, now=NOW)

    assert (snapshot.origin_facing, snapshot.ip_ranges, snapshot.networks) == ((), None, ())
    assert from_ip_ranges(None) == ((), None)
    assert from_ip_ranges({"prefixes": RANGES["prefixes"]}) == (("130.176.0.0/18",), None)


def test_the_empty_snapshot_is_fail_safe() -> None:
    assert EMPTY.demand == {} and EMPTY.networks == ()
    assert EMPTY.allow_list.allowed() == {}


def test_a_snapshot_is_strict() -> None:
    snapshot = snapshot_at(NOW)
    data = snapshot.model_dump(mode="json")

    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "origin_facing": ["not-a-network"]})
    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "unknown": 1})
    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "version": "1"})
    assert type(snapshot).model_validate_json(snapshot.model_dump_json()) == snapshot


def test_resource_names() -> None:
    assert [e.function_suffix for e in names.Endpoint] == ["policy", "select", "ownbook"]
    assert f"{names.RECORD}.fastm.fast.internal" == pds_host("fastm")
    assert names.table_name("fastm") == "fastm-pds"
    assert names.census_gate("fastm") == "fastm-pds-census-gate"
    assert names.publisher_alarm("fastm") == "fastm-pds-publisher-dead-letters"
    assert names.dead_letter_alarm("fastm", "delivery") == "fastm-pds-delivery-dead-letters"
    assert names.parameter_name("fastm", names.Parameter.LADDER_VERSION) == "/fastm/pds/ladder-version"
    assert (names.decision_stream("fastm", "policy"), names.decision_table("policy")) == (
        "fastm-pds-policy",
        "pds_policy",
    )


LINE = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal(8),
    budget=Decimal(100),
    tier0=True,
)
DIRECT1 = Creative(
    crid="direct1", creative_id="demo-direct1", hls_uri="https://cdn.example/ads/x/direct1.m3u8", duration_ms=30030
)
HOUSE1 = Creative(
    crid="house1", creative_id="house-ad-1", hls_uri="https://cdn.example/ads/x/house1.m3u8", duration_ms=30030
)


def test_the_snapshot_carries_the_own_book() -> None:
    base = BASE.model_copy(update={"lines": (LINE,)})
    own = Book(ladder_version="standard@843ed1d05a", lines=(LINE,), creatives=(DIRECT1,), house=(HOUSE1,))
    snapshot = build(base, origin_facing=NETWORKS, ip_ranges=SYNC, own=own, now=NOW)
    loaded = Snapshot.model_validate_json(snapshot.model_dump_json())

    assert loaded.book == own
    assert loaded.book.tier0_deals == frozenset({"fast-direct-1"})
    assert digest(loaded) != digest(build(base, origin_facing=NETWORKS, now=NOW)), "the creatives are content"
    assert EMPTY.book == Book()
    later = build(base, origin_facing=NETWORKS, own=own.model_copy(update={"lines": ()}), now=NOW)
    assert later.book.lines == (LINE,), "the lines are the deployment's"


def test_a_snapshot_whose_book_is_inconsistent_is_refused() -> None:
    data = snapshot_at(NOW).model_dump(mode="json")
    with pytest.raises(ValidationError, match="creative ids must be unique"):
        Snapshot.model_validate({**data, "house": [HOUSE1.model_dump(), HOUSE1.model_dump()]})
