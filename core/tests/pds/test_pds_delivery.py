"""Reservations and delivery (M2-03): keys, shards and our tracker in MediaTailor's BEACON_FIRED events."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from fast_channels.core.ledger import CHAIN_ID_PATTERN
from fast_channels.core.pbs_config import PbsSettings
from fast_channels.core.pds.delivery import (
    BUDGET_SHARDS,
    CDN_LOG_EVERY,
    CDN_LOG_LAG,
    Beacon,
    Ignored,
    Source,
    beacon,
    budget_key,
    cap_key,
    cdn_beacon,
    cdn_log_query,
    cdn_log_window,
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


TRACKER = f"https://{HOST}/ads/b"
"""Our tracker's prefix on the CDN (``pbs_config.TRACKER_PREFIX``)."""


def test_the_tracker_the_pds_renders_is_the_one_it_reads() -> None:
    uri = tracker_url(TRACKER, f"{SID}.42", "sim3", enr=1)
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


# The CDN-log source (W-2 1.32).

SCHEDULED = datetime(2026, 11, 5, 20, 0, 13, tzinfo=UTC)


def cdn_row(uri: str, **overrides: str | None) -> dict[str, str | None]:
    """A row of the CDN-log query for a request of ``uri``."""
    parts = urlsplit(uri)
    row: dict[str, str | None] = {
        "timestamp_ms": "1793908801100",  # 2026-11-05T20:00:01.100Z
        "cs_uri_stem": parts.path,
        "cs_uri_query": parts.query or "-",
        "sc_status": "200",
        "pc": "fastm-demo-p-a",
    }
    return row | overrides


def query_hours(query: str) -> tuple[set[str], set[str]]:
    """The log hours (``yyyy/MM/dd/HH``) and select-record hours a CDN-log query reads."""
    logs = {
        "/".join(h)
        for h in re.findall(r"year = '(\d+)' AND month = '(\d+)' AND day = '(\d+)' AND hour = '(\d+)'", query)
    }
    [selects] = re.findall(r"datehour IN \(([^)]*)\)", query)
    return logs, set(re.findall(r"'([^']+)'", selects))


def test_a_cdn_log_row_of_our_tracker_is_the_impression_its_beacon_is() -> None:
    uri = tracker_url(TRACKER, f"{SID}.1234", "direct1", enr=1)
    found = cdn_beacon(cdn_row(uri), host=HOST)
    fired_by_mediatailor = beacon(fired(uri), host=HOST)

    assert found == Beacon(
        sid=SID,
        evid="1234",
        crid="direct1",
        enr=1,
        at=datetime(2026, 11, 5, 20, 0, 1, 100000, tzinfo=UTC),
        configuration="fastm-demo-p-a",
        status=200,
        source=Source.CDN_LOG,
    )
    assert isinstance(fired_by_mediatailor, Beacon) and fired_by_mediatailor.source is Source.ADS_LOG
    ads = fired_by_mediatailor
    assert (found.decision_id, found.crid, found.enr) == (ads.decision_id, ads.crid, ads.enr)


@pytest.mark.parametrize(
    ("row", "why"),
    [
        (cdn_row(f"https://{HOST}/ads/b.gif"), Ignored.OTHER_TRACKER),
        (cdn_row(f"https://{HOST}/ads/b.gif?evt=impression&cr=house-ad-1"), Ignored.OTHER_TRACKER),
        (cdn_row(f"https://{HOST}/ads/b.gif?d={SID}&c=x&e=impression&enr=0"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b.gif?d={SID}.1&c=bad%20crid&e=impression&enr=0"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b.gif?d={SID}.1&c=x&e=start&enr=0"), Ignored.OTHER_EVENT),
        (cdn_row(f"https://{HOST}/ads/b.gif?d={SID}.1&c=x&e=impression&enr=0", timestamp_ms="-"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b.gif?d={SID}.1&c=x&e=impression&enr=0", timestamp_ms=None), Ignored.MALFORMED),
        ({"cs_uri_query": None}, Ignored.OTHER_TRACKER),
        (cdn_row(f"https://{HOST}/ads/b/{SID}/x/0.gif"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b/{SID}.1/x/2.gif"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b/{SID}.1/bad%20crid/0.gif"), Ignored.MALFORMED),
        (cdn_row(f"https://{HOST}/ads/b/{SID}.1/x/0.png"), Ignored.OTHER_TRACKER),
        (cdn_row(f"https://{HOST}/ads/b/{SID}.1/0.gif"), Ignored.OTHER_TRACKER),
    ],
)
def test_cdn_log_rows_that_are_not_an_impression_of_ours_are_ignored(row: dict[str, str | None], why: Ignored) -> None:
    assert cdn_beacon(row, host=HOST) is why


def test_a_cdn_log_row_without_a_status_or_configuration_is_still_the_impression() -> None:
    uri = tracker_url(TRACKER, f"{SID}.7", "sim1")
    found = cdn_beacon(cdn_row(uri, sc_status="-", pc=None), host=HOST.upper())

    assert isinstance(found, Beacon) and (found.status, found.configuration) == (None, "")


def test_consecutive_runs_read_consecutive_windows_ten_minutes_after_their_files_arrive() -> None:
    first, second = cdn_log_window(SCHEDULED), cdn_log_window(SCHEDULED + CDN_LOG_EVERY)

    assert first == (SCHEDULED - CDN_LOG_LAG - CDN_LOG_EVERY, SCHEDULED - CDN_LOG_LAG)
    assert second[0] == first[1]
    assert cdn_log_window(SCHEDULED.astimezone(timezone(timedelta(hours=1)))) == first


def test_the_query_reads_the_windows_files_and_counts_only_ads_a_select_record_placed() -> None:
    start, end = cdn_log_window(SCHEDULED)
    query = cdn_log_query(start, end, cdn_table="fastm_pds.cdn_access", select_table="fastm_lake.pds_select")

    assert "FROM fastm_pds.cdn_access" in query and "FROM fastm_lake.pds_select" in query
    assert "(cs_uri_stem LIKE '/ads/b/%' OR cs_uri_stem = '/ads/b.gif')" in query
    assert "regexp_extract(cs_uri_stem, '^/ads/b/([^/]+)/([^/]+)/[^/]+\\.gif$', 1)" in query
    assert "\"$file_modified_time\" >= from_iso8601_timestamp('2026-11-05T19:45:13.000+00:00')" in query
    assert "\"$file_modified_time\" < from_iso8601_timestamp('2026-11-05T19:50:13.000+00:00')" in query
    assert "record = 'pds.select' AND NOT probe" in query
    assert "json_extract_scalar(bid, '$.outcome') IN ('won', 'standalone')" in query
    assert "p.decision_id = t.d AND p.crid = t.c" in query
    logs, selects = query_hours(query)
    assert logs == {"2026/11/05/17", "2026/11/05/18", "2026/11/05/19"}
    assert min(selects) == "2026/11/05/16" and max(selects) == "2026/11/05/19" and len(selects) == 4


def test_a_log_file_forty_minutes_late_is_read_by_exactly_one_run() -> None:
    """CloudFront delivers a file about 5 minutes after its requests; this one arrives 45 minutes after."""
    requested = datetime(2026, 11, 5, 19, 58, 30, tzinfo=UTC)
    arrived = requested + timedelta(minutes=45)
    readers = []
    for n in range(48):  # every run of 4 hours
        scheduled = datetime(2026, 11, 5, 19, 0, 13, tzinfo=UTC) + n * CDN_LOG_EVERY
        start, end = cdn_log_window(scheduled)
        logs, selects = query_hours(cdn_log_query(start, end, cdn_table="c", select_table="s"))
        if start <= arrived < end:
            readers.append(scheduled)
            assert f"{requested:%Y/%m/%d/%H}" in logs  # its requests' hour
            assert f"{requested - timedelta(minutes=5):%Y/%m/%d/%H}" in selects  # their breaks' select records

    assert readers == [datetime(2026, 11, 5, 20, 55, 13, tzinfo=UTC)]  # window [20:40:13, 20:45:13)


def test_a_redriven_run_reads_its_own_window_again() -> None:
    assert cdn_log_window(SCHEDULED) == cdn_log_window(datetime.fromisoformat("2026-11-05T20:00:13Z"))


# The tracker's path form, and the former query form during the transition.


def test_the_path_and_the_former_query_form_of_a_firing_are_one_impression() -> None:
    """A pod rendered before the change still fires the query form: both read as the same ad of the same break, so
    the dedupe keys count it once."""
    path = tracker_url(TRACKER, f"{SID}.1234", "direct1", enr=1)
    query = f"https://{HOST}/ads/b.gif?d={SID}.1234&c=direct1&e=impression&enr=1"
    found = [beacon(fired(uri), host=HOST) for uri in (path, query)]
    rows = [cdn_beacon(cdn_row(uri), host=HOST) for uri in (path, query)]

    keys = {(b.decision_id, b.crid, b.enr) for b in (*found, *rows) if isinstance(b, Beacon)}
    assert keys == {(f"{SID}.1234", "direct1", 1)}
    assert path == f"{TRACKER}/{SID}.1234/direct1/1.gif"


def test_with_the_identity_module_the_logs_have_no_query_and_the_path_still_counts() -> None:
    path = tracker_url(TRACKER, f"{SID}.9", "sim2")
    query = f"https://{HOST}/ads/b.gif?d={SID}.9&c=sim2&e=impression&enr=0"

    assert isinstance(cdn_beacon(cdn_row(path, cs_uri_query=None), host=HOST), Beacon)
    assert cdn_beacon(cdn_row(query, cs_uri_query=None), host=HOST) is Ignored.OTHER_TRACKER


def test_the_longest_creative_id_round_trips_through_the_path() -> None:
    crid = "A-z_0.9" * 9 + "-"  # 64 characters, the most NAME_PATTERN allows
    uri = tracker_url(TRACKER, f"{'f' * 64}.{2**31 - 1}", crid, enr=1)
    found = beacon(fired(uri, sessionId="f" * 64), host=HOST)

    assert len(crid) == 64 and len(uri) < 256
    assert isinstance(found, Beacon) and (found.crid, found.evid, found.enr) == (crid, str(2**31 - 1), 1)


def test_an_encoded_path_segment_is_decoded_and_never_splits_the_path() -> None:
    """``%2F`` is one segment's slash: the ad ``a/b`` is not a name, so it is malformed, not another path."""
    assert beacon(fired(f"https://{HOST}/ads/b/{SID}.1/a%2Fb/0.gif"), host=HOST) is Ignored.MALFORMED
    assert beacon(fired(f"https://{HOST}/ads/b/{SID}.1/a/b/0.gif"), host=HOST) is Ignored.OTHER_TRACKER


def test_the_module_and_the_pds_render_the_same_path() -> None:
    template = PbsSettings(
        prefix="fastm",
        simulator=True,
        server_instances=2,
        tracker_base_url=f"https://{HOST}",
        publisher_domain="media.example.com",
    ).tracker_template

    assert template == f"https://{HOST}/ads/b/{{sid}}.{{evid}}/{{crid}}/{{enr}}.gif"
    rendered = template.format(sid=SID, evid="1234", crid="direct1", enr=0)
    assert rendered == tracker_url(TRACKER, f"{SID}.1234", "direct1")
