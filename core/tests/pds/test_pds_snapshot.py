from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

from fast_channels.core.pbs_config import pds_host
from fast_channels.core.pds import names
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
    assert (names.decision_stream("fastm", "policy"), names.decision_table("policy")) == (
        "fastm-pds-policy",
        "pds_policy",
    )
