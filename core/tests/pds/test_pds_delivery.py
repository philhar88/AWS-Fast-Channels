"""Reservations and delivery (M2-03): keys, shards and our tracker in MediaTailor's BEACON_FIRED events."""

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.ledger import CHAIN_ID_PATTERN
from fast_channels.core.pds.delivery import (
    BUDGET_SHARDS,
    Beacon,
    Ignored,
    beacon,
    budget_key,
    cap_key,
    chain_id,
    fill_key,
    home_shard,
    impression_key,
    money,
    reservation_key,
    shard_caps,
)
from fast_channels.core.pds.pod import tracker_url

FIXTURES = Path(__file__).parents[3] / "functions" / "tests" / "pds" / "fixtures" / "beacon_fired.jsonl"
HOST = "d111111abcdef8.cloudfront.net"
SID = "0a1e6454-90e6-423a-80c6-5e02eff31cdc"


def recorded() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURES.read_text().splitlines()]


def fired(uri: str, **overrides: Any) -> dict[str, Any]:
    event = recorded()[0]
    event["beaconInfo"] = {**event["beaconInfo"], "beaconUri": uri}
    return {**event, **overrides}


def test_our_tracker_in_a_recorded_beacon_is_an_impression_of_one_ad_of_one_break() -> None:
    found = beacon(recorded()[0], host=HOST)

    assert found == Beacon(
        sid=SID,
        evid="1234",
        crid="direct1",
        enr=0,
        at=datetime(2026, 10, 29, 15, 0, 1, 100000, tzinfo=UTC),
        configuration="fastm-demo-p-a",
        status=200,
    )
    assert isinstance(found, Beacon) and found.decision_id == f"{SID}.1234"


def test_the_fixtures_hold_five_firings_of_ours_and_three_of_other_trackers() -> None:
    outcomes = [beacon(event, host=HOST) for event in recorded()]

    assert [o.crid if isinstance(o, Beacon) else o for o in outcomes] == [
        "direct1",
        "house1",
        "sim2",
        "direct1",
        Ignored.OTHER_TRACKER,
        Ignored.OTHER_TRACKER,
        Ignored.OTHER_TRACKER,
        "direct2",
    ]


def test_the_tracker_the_pds_renders_is_the_one_it_reads() -> None:
    uri = tracker_url(f"https://{HOST}/ads/b.gif", f"{SID}.42", "sim3", enr=1)
    found = beacon(fired(uri), host=HOST.upper())

    assert isinstance(found, Beacon)
    assert (found.decision_id, found.crid, found.enr) == (f"{SID}.42", "sim3", 1)


@pytest.mark.parametrize(
    ("uri", "why"),
    [
        (f"http://{HOST}/ads/b.gif?d={SID}.1&c=x&e=impression&enr=0", Ignored.OTHER_TRACKER),
        (f"https://other.example/ads/b.gif?d={SID}.1&c=x&e=impression&enr=0", Ignored.OTHER_TRACKER),
        (f"https://{HOST}/ads/other.gif?d={SID}.1&c=x&e=impression&enr=0", Ignored.OTHER_TRACKER),
        (f"https://{HOST}/ads/b.gif?evt=impression&cr=house-ad-1", Ignored.OTHER_TRACKER),
        (f"https://{HOST}/ads/b.gif?d={SID}&c=x&e=impression&enr=0", Ignored.MALFORMED),
        (f"https://{HOST}/ads/b.gif?d={SID}.1&d={SID}.2&c=x&e=impression&enr=0", Ignored.MALFORMED),
        (f"https://{HOST}/ads/b.gif?d={SID}.1&c=x&e=impression&enr=2", Ignored.MALFORMED),
        (f"https://{HOST}/ads/b.gif?d={SID}.1&e=impression&enr=0", Ignored.MALFORMED),
        (f"https://{HOST}/ads/b.gif?d={SID}.1&c=bad%20crid&e=impression&enr=0", Ignored.MALFORMED),
        (f"https://{HOST}/ads/b.gif?d={SID}.1&c=x&e=start&enr=0", Ignored.OTHER_EVENT),
        (f"https://{HOST}/ads/b.gif?d=another-session.1&c=x&e=impression&enr=0", Ignored.SESSION_MISMATCH),
    ],
)
def test_anything_else_is_not_an_impression_of_ours(uri: str, why: Ignored) -> None:
    assert beacon(fired(uri), host=HOST) is why


@pytest.mark.parametrize(
    "event",
    [
        {"eventType": "FILLED_AVAIL"},
        {"eventType": "BEACON_FIRED"},
        {"eventType": "BEACON_FIRED", "beaconInfo": {"trackingEvent": "impression"}},
        {"eventType": "BEACON_FIRED", "beaconInfo": "x"},
    ],
)
def test_events_without_a_fired_beacon_are_skipped(event: dict[str, Any]) -> None:
    assert beacon(event, host=HOST) is Ignored.NOT_BEACON


def test_a_firing_without_a_readable_time_is_malformed() -> None:
    event = fired(recorded()[0]["beaconInfo"]["beaconUri"], eventTimestamp="yesterday")
    assert beacon(event, host=HOST) is Ignored.MALFORMED
    event = fired(recorded()[0]["beaconInfo"]["beaconUri"])
    event["beaconInfo"]["beaconHttpResponseCode"] = True
    found = beacon(event, host=HOST)
    assert isinstance(found, Beacon) and found.status is None


def test_keys_name_one_ad_of_one_break() -> None:
    decision = f"{SID}.1234"
    assert reservation_key(decision, "direct1") == {"pk": f"RES#{decision}", "sk": "direct1"}
    assert impression_key(decision, "direct1") == {"pk": f"IMP#{decision}", "sk": "direct1"}
    assert fill_key(decision) == {"pk": f"FILL#{decision}", "sk": "-"}
    assert budget_key("direct-1", 3) == {"pk": "BUDGET#direct-1", "sk": "S#3"}
    assert cap_key(SID, "direct-2") == {"pk": f"CAP#{SID}", "sk": "direct-2"}
    assert re.fullmatch(CHAIN_ID_PATTERN, chain_id("direct-1")) and chain_id("direct-1") == "pds/line/direct-1"


@pytest.mark.parametrize("budget", ["1", "100", "100.01", "0.03", "7"])
def test_the_shards_split_the_budget_exactly(budget: str) -> None:
    caps = shard_caps(Decimal(budget))
    assert len(caps) == BUDGET_SHARDS
    assert sum(caps) == Decimal(budget)
    assert all(cap >= 0 for cap in caps)


def test_a_break_reserves_from_a_stable_shard() -> None:
    shards = {home_shard(f"{SID}.{n}") for n in range(200)}
    assert shards == set(range(BUDGET_SHARDS))
    assert home_shard(f"{SID}.1") == home_shard(f"{SID}.1")


def test_ledger_amounts_are_decimal_strings() -> None:
    assert money(Decimal("0.008")) == "0.008000"
    assert money(Decimal(1) / 3) == "0.333333"
