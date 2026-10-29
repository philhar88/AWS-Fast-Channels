from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import ip_network

import pytest
from pydantic import ValidationError

from fast_channels.core.pds import names
from fast_channels.core.pds.policy import Demand
from fast_channels.core.pds.snapshot import (
    EMPTY,
    HISTORY_PREFIX,
    SnapshotBase,
    build,
    demo_demand,
    digest,
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


def test_a_snapshot_carries_the_configuration_and_the_origin_facing_networks() -> None:
    snapshot = build(BASE, RANGES, now=NOW)

    assert snapshot.version.startswith("20261029T130405Z-")
    assert snapshot.origin_facing == ("130.176.0.0/18", "2600:9000:1000::/36")
    assert snapshot.networks == (ip_network("130.176.0.0/18"), ip_network("2600:9000:1000::/36"))
    assert snapshot.ip_ranges == "1793278000"
    assert snapshot.demand == {"amt": {"placementId": "fastm-demo"}}
    assert snapshot.allow_list == Demand({"amt": {"placementId": "fastm-demo"}}, frozenset({"other"}))
    assert snapshot.floor_cpm == Decimal("0.5")
    assert history_key(snapshot) == f"{HISTORY_PREFIX}{snapshot.version}.json"


def test_the_digest_follows_the_content_not_the_time() -> None:
    first, later = build(BASE, RANGES, now=NOW), build(BASE, RANGES, now=datetime(2026, 10, 30, tzinfo=UTC))
    changed = build(BASE.model_copy(update={"floor_cpm": Decimal(1)}), RANGES, now=NOW)

    assert first.version != later.version
    assert digest(first) == digest(later) != digest(changed)


def test_without_ip_ranges_there_is_no_network() -> None:
    snapshot = build(BASE, None, now=NOW)

    assert (snapshot.origin_facing, snapshot.ip_ranges, snapshot.networks) == ((), None, ())
    assert build(BASE, {"prefixes": RANGES["prefixes"]}, now=NOW).ip_ranges is None


def test_the_empty_snapshot_is_fail_safe() -> None:
    assert EMPTY.demand == {} and EMPTY.networks == ()
    assert EMPTY.allow_list.allowed() == {}


def test_a_snapshot_is_strict() -> None:
    snapshot = build(BASE, RANGES, now=NOW)
    data = snapshot.model_dump(mode="json")

    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "origin_facing": ["not-a-network"]})
    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "unknown": 1})
    with pytest.raises(ValidationError):
        type(snapshot).model_validate({**data, "version": "1"})
    assert type(snapshot).model_validate_json(snapshot.model_dump_json()) == snapshot


def test_resource_names() -> None:
    assert [names.function_name("fastm", e) for e in names.Endpoint] == [
        "fastm-pds-policy",
        "fastm-pds-select",
        "fastm-pds-ownbook",
    ]
    assert names.table_name("fastm") == "fastm-pds"
    assert names.census_gate("fastm") == "fastm-pds-census-gate"
    assert names.publisher_function("fastm") == "fastm-pds-publisher"
    assert (names.decision_stream("fastm", "policy"), names.decision_table("policy")) == (
        "fastm-pds-policy",
        "pds_policy",
    )
