"""The prefetch e2e's log readers (``harness_prefetch``), against vended-log shapes seen live (P0-02, P0-11)."""

from datetime import UTC, datetime, timedelta

import pytest
from harness_prefetch import Avail, Request, avails, requests, spread, timestamp

START = datetime(2026, 10, 30, 12, tzinfo=UTC)
URL = "https://pbs.example/openrtb2/auction?evid={evid}&upid=e2e-pattern&dur={dur}"


def request_event(*, evid: int, at: str, schedule: str | None = None, dur: int = 60060) -> dict[str, object]:
    event: dict[str, object] = {
        "eventType": "MAKING_ADS_REQUEST",
        "eventTimestamp": at,
        "originId": "fastcif-demo-p-b",
        "sessionId": "s1",
        "adsRequestUrl": URL.format(evid=evid, dur=dur),
    }
    if schedule:
        event["prefetchScheduleName"] = schedule
    return event


def test_timestamps_carry_nanoseconds() -> None:
    assert timestamp("2026-10-27T21:10:11.717529762Z") == datetime(2026, 10, 27, 21, 10, 11, 717529, tzinfo=UTC)
    assert timestamp("2026-10-27T21:10:11Z") == datetime(2026, 10, 27, 21, 10, 11, tzinfo=UTC)
    with pytest.raises(ValueError, match="not an event timestamp"):
        timestamp("yesterday")


def test_requests_name_their_break() -> None:
    found = requests(
        [
            request_event(evid=7, at="2026-10-30T11:58:31.5Z", schedule="pf-7", dur=60000),
            request_event(evid=8, at="2026-10-30T12:00:00.25Z"),
            {"eventType": "VAST_RESPONSE", "eventTimestamp": "2026-10-30T12:00:01Z", "originId": "x"},
        ]
    )
    assert found == [
        Request("fastcif-demo-p-b", "s1", 7, True, START - timedelta(seconds=88.5)),
        Request("fastcif-demo-p-b", "s1", 8, False, START + timedelta(seconds=0.25)),
    ]


def test_a_prefetched_avail_is_read_from_its_schedule_not_its_stale_url() -> None:
    event = {
        "eventType": "FILLED_AVAIL",
        "originId": "fastcif-demo-p-b",
        "sessionId": "s1",
        "prefetchScheduleName": "pf-7",
        "adsRequestUrl": URL.format(evid=6, dur=60060),  # the session's previous real-time request
        "avail": {
            "filledDuration": 60.06,
            "skippedAds": [{"skippedReason": "NEW_CREATIVE"}, {"skippedReason": "DURATION_MISMATCH"}],
        },
    }
    assert avails([event]) == [Avail("fastcif-demo-p-b", "s1", 7, True, 60.06, ("NEW_CREATIVE",))]


def test_the_spread_of_a_herd() -> None:
    at = [START + timedelta(seconds=s) for s in range(-120, -29)]  # one request a second over 91 s
    measured = spread(at, start=START)
    assert measured is not None
    assert (measured.count, measured.first, measured.last) == (91, -120.0, -30.0)
    assert measured.width == pytest.approx(81.0)
    assert spread([], start=START) is None
    single = spread([START], start=START)
    assert single is not None and single.width == 0
