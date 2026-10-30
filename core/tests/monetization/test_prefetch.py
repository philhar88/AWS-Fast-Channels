"""Prefetch schedules (M3-06): the breaks of Channel Assembly's schedule of record, the SINGLE schedule of each, and
the plan against a sibling's existing schedules (create-only, re-planned breaks deleted, RECURRING never mixed)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.monetization.playback import PlaybackConfiguration, sibling_playback_configuration
from fast_channels.core.monetization.prefetch import (
    ACTIVE_LIMIT,
    Break,
    PrefetchPolicy,
    breaks_of,
    content_id_of_upid,
    event_id_of,
    plan,
    schedule_name,
    schedule_request,
    schedule_tags,
)
from fast_channels.core.monetization.validators import check_prefetch_schedule
from fast_channels.core.timeline import upid

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
GRID = (30030, 60060, 90090, 120120)
POLICY = PrefetchPolicy()
TAGS = schedule_tags("fastcif")
SIBLING = "fastcif-demo-p-b"


def program(name: str, *breaks: tuple[int, str]) -> dict[str, Any]:
    """A ``DescribeProgram`` answer whose ad breaks are TIME_SIGNALs as ``core.specs.ad_breaks`` builds them."""
    return {
        "ProgramName": name,
        "AdBreaks": [
            {
                "OffsetMillis": 240_000 * n,
                "MessageType": "TIME_SIGNAL",
                "TimeSignalMessage": {
                    "SegmentationDescriptors": [
                        {
                            "SegmentationEventId": event_id,
                            "SegmentationUpidType": 12,
                            "SegmentationUpid": upid(content),
                            "SegmentationTypeId": 52,
                            "SegmentNum": n,
                            "SegmentsExpected": len(breaks),
                        }
                    ]
                },
            }
            for n, (event_id, content) in enumerate(breaks, start=1)
        ],
    }


def entry(name: str, *starts: datetime, seconds: int = 60) -> dict[str, Any]:
    """A ``GetChannelSchedule`` item."""
    return {
        "ProgramName": name,
        "ScheduleEntryType": "PROGRAM",
        "ApproximateStartTime": starts[0] - timedelta(minutes=4),
        "ScheduleAdBreaks": [{"ApproximateDurationSeconds": seconds, "ApproximateStartTime": s} for s in starts],
    }


def at(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


def brk(event_id: int, minutes: float, content: str = "sintel", duration_ms: int = 60060) -> Break:
    return Break(event_id, content, at(minutes), duration_ms, f"p-{content}")


def existing(item: Break, *, consumption_start: datetime | None = None, kind: str = "SINGLE") -> dict[str, Any]:
    """A ``ListPrefetchSchedules`` item, its times in whole seconds as MediaTailor stores them (P0-02)."""
    start = consumption_start or item.start - timedelta(seconds=POLICY.consumption_lead_s)
    end = item.start + timedelta(milliseconds=item.duration_ms, seconds=POLICY.consumption_tail_s)
    retrieval = item.start - timedelta(seconds=POLICY.retrieval_lead_s)
    return {
        "Name": item.name,
        "ScheduleType": kind,
        "Retrieval": {"StartTime": retrieval.replace(microsecond=0)},
        "Consumption": {"StartTime": start.replace(microsecond=0), "EndTime": end.replace(microsecond=0)},
    }


# --- the schedule of record -----------------------------------------------------------------------------------------


def test_breaks_come_from_the_schedule_with_their_programs_ids() -> None:
    entries = [
        entry("20261030T120000-tears-of-steel", at(10), at(14)),
        {"ProgramName": "filler", "ScheduleEntryType": "FILLER_SLATE", "ScheduleAdBreaks": []},
        entry("20261030T115000-sintel", at(3)),
    ]
    programs = {
        "20261030T120000-tears-of-steel": program("t", (687826497, "tears-of-steel"), (695720452, "tears-of-steel")),
        "20261030T115000-sintel": program("s", (1027354323, "sintel")),
    }
    breaks, skipped = breaks_of(entries, programs, breaks_ms=GRID)
    assert skipped == []
    assert [(b.event_id, b.upid, b.start, b.duration_ms) for b in breaks] == [
        (1027354323, "sintel", at(3), 60060),
        (687826497, "tears-of-steel", at(10), 60060),
        (695720452, "tears-of-steel", at(14), 60060),
    ]


@pytest.mark.parametrize(
    ("entries", "programs", "why"),
    [
        ([entry("p", at(3))], {}, "p: not described"),
        ([entry("p", at(3), at(6))], {"p": program("p", (1, "sintel"))}, "p: 2 scheduled breaks, 1 ad breaks"),
        ([entry("p", at(3), seconds=45)], {"p": program("p", (1, "sintel"))}, "no event id, UPID or schedule length"),
    ],
)
def test_breaks_that_cannot_be_matched_are_left_out(
    entries: list[dict[str, Any]], programs: dict[str, Any], why: str
) -> None:
    breaks, skipped = breaks_of(entries, programs, breaks_ms=GRID)
    assert breaks == [] and len(skipped) == 1 and why in skipped[0]


def test_a_break_without_a_upid_is_left_to_real_time_insertion() -> None:
    signal = program("p", (1, "sintel"))
    signal["AdBreaks"][0]["TimeSignalMessage"]["SegmentationDescriptors"][0]["SegmentationUpidType"] = 1
    breaks, skipped = breaks_of([entry("p", at(3))], {"p": signal}, breaks_ms=GRID)
    assert breaks == [] and "no event id, UPID" in skipped[0]


@pytest.mark.parametrize(
    ("kind", "payload", "expected"),
    [(12, upid("tears-of-steel"), "tears-of-steel"), (12, "ZZ", ""), (12, b"XYZ1x".hex(), ""), (9, upid("a"), "")],
)
def test_the_content_id_of_a_upid(kind: int, payload: str, expected: str) -> None:
    assert content_id_of_upid(kind, payload) == expected


def test_names() -> None:
    assert schedule_name(1027354323) == "pf-1027354323"
    assert event_id_of("pf-1027354323") == 1027354323
    assert event_id_of("someone-elses") is None and event_id_of("pf-") is None


# --- one schedule ---------------------------------------------------------------------------------------------------


def test_a_schedule_carries_r_s1_and_spreads_its_retrieval() -> None:
    item = brk(1027354323, 10)
    request = schedule_request(item, configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert request == {
        "Name": "pf-1027354323",
        "PlaybackConfigurationName": SIBLING,
        "ScheduleType": "SINGLE",
        "Retrieval": {
            "StartTime": at(10) - timedelta(seconds=150),
            "EndTime": at(10) - timedelta(seconds=30),
            "DynamicVariables": {
                "scte.event_id": "1027354323",
                "scte.segmentation_upid": "sintel",
                "session.avail_duration_ms": "60060",
                "session.avail_duration_secs": "60",
            },
            "TrafficShapingType": "RETRIEVAL_WINDOW",
            "TrafficShapingRetrievalWindow": {"RetrievalWindowDurationSeconds": 90},
        },
        "Consumption": {
            "StartTime": at(10) - timedelta(seconds=20),
            "EndTime": at(10) + timedelta(milliseconds=60060, seconds=10),
            "AvailMatchingCriteria": [{"DynamicVariable": "scte.event_id", "Operator": "EQUALS"}],
        },
        "Tags": {"fast:app": "fastcif", "fast:owner": "release-controller"},
    }
    assert check_prefetch_schedule(request) == []


def test_a_late_schedule_opens_now_and_shapes_within_what_is_left() -> None:
    item = brk(1, 1.5)  # 90 s ahead: retrieval would have opened 60 s ago
    retrieval = schedule_request(item, configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)["Retrieval"]
    assert retrieval["StartTime"] == NOW and retrieval["EndTime"] == NOW + timedelta(seconds=60)
    assert retrieval["TrafficShapingRetrievalWindow"] == {"RetrievalWindowDurationSeconds": 60}


def test_without_a_shaping_window_requests_go_out_as_sessions_arrive() -> None:
    policy = PrefetchPolicy(shaping_window_s=0)
    retrieval = schedule_request(brk(1, 10), configuration=SIBLING, policy=policy, now=NOW, tags=TAGS)["Retrieval"]
    assert "TrafficShapingType" not in retrieval and "TrafficShapingRetrievalWindow" not in retrieval


@pytest.mark.parametrize(("duration_ms", "seconds"), [(30030, "30"), (60060, "60"), (90090, "90"), (120120, "120")])
def test_the_stored_request_length(duration_ms: int, seconds: str) -> None:
    request = schedule_request(
        brk(1, 10, duration_ms=duration_ms), configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS
    )
    assert request["Retrieval"]["DynamicVariables"]["session.avail_duration_secs"] == seconds


# --- the plan -------------------------------------------------------------------------------------------------------


def test_create_only_the_breaks_without_a_schedule() -> None:
    breaks = [brk(1, 5), brk(2, 9), brk(3, 13)]
    result = plan(breaks, [existing(breaks[0])], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert [r["Name"] for r in result.create] == ["pf-2", "pf-3"]
    assert (result.kept, result.delete, result.refused) == (["pf-1"], [], None)


def test_the_horizon_and_the_quota() -> None:
    breaks = [brk(n, 5 + 4 * n) for n in range(1, 40)]
    first = plan(breaks, [], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert [r["Name"] for r in first.create] == [f"pf-{n}" for n in range(1, 21)]
    crowded = [existing(brk(1000 + n, 5 + n)) for n in range(ACTIVE_LIMIT - 1)]  # ours, all for re-planned breaks
    full = plan(breaks[:2], crowded, configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert len(full.delete) == ACTIVE_LIMIT - 1 and [r["Name"] for r in full.create] == ["pf-1", "pf-2"]


def test_a_break_too_close_to_retrieve_is_left_to_real_time_insertion() -> None:
    result = plan([brk(1, 0.9), brk(2, 1.1)], [], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert [r["Name"] for r in result.create] == ["pf-2"]  # 54 s ahead is below 30 s + 30 s; 66 s is not


def test_a_re_planned_break_loses_its_schedule() -> None:
    kept, gone = brk(1, 5), brk(2, 9)
    result = plan([kept], [existing(kept), existing(gone)], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert (result.kept, result.delete, result.create) == (["pf-1"], ["pf-2"], [])


def test_a_moved_break_is_deleted_and_created_again() -> None:
    moved = brk(1, 8)
    stale = existing(brk(1, 5))
    result = plan([moved], [stale], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert result.delete == ["pf-1"] and [r["Name"] for r in result.create] == ["pf-1"]
    assert result.create[0]["Consumption"]["StartTime"] == at(8) - timedelta(seconds=20)


def test_a_retrieved_break_keeps_its_schedule_over_a_small_start_shift() -> None:
    """fastcij run 5 (2026-10-30), pf-1135467694 on fastcij-demo-p-a: created for a break Channel Assembly gave as
    13:31:53.9 (stored: retrieval 13:29:23, consumption 13:31:33 to 13:32:33). At 13:30:51, with every session
    retrieved, the break was at 13:31:55.1: 2.1 s from the stored start, over the old 2 s. Its schedule was deleted and
    created again, and every session made a second, real-time auction at the break."""
    first = Break(1135467694, "sintel", datetime(2026, 10, 30, 13, 31, 53, 900_000, tzinfo=UTC), 30030, "p-sintel")
    shifted = Break(first.event_id, first.upid, first.start + timedelta(milliseconds=1200), 30030, first.program)
    now = datetime(2026, 10, 30, 13, 30, 51, tzinfo=UTC)
    result = plan([shifted], [existing(first)], configuration=SIBLING, policy=POLICY, now=now, tags=TAGS)
    assert (result.kept, result.delete, result.create) == (["pf-1135467694"], [], [])


@pytest.mark.parametrize(("shift_s", "moved"), [(2.1, False), (2.9, False), (4.0, True), (-4.0, True)])
def test_before_retrieval_a_break_moved_only_beyond_half_a_segment(shift_s: float, moved: bool) -> None:
    """Before retrieval opens a new schedule costs nothing, so a real move re-creates it. Starts are compared in whole
    seconds (MediaTailor's resolution) against half a 6.006 s segment."""
    first = brk(1, 8)
    shifted = Break(1, first.upid, first.start + timedelta(seconds=shift_s), first.duration_ms, first.program)
    result = plan([shifted], [existing(first)], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert (result.delete == ["pf-1"]) is moved and (result.kept == ["pf-1"]) is not moved


@pytest.mark.parametrize(("shift_s", "moved"), [(15.0, False), (-19.0, False), (-25.0, True), (75.0, True)])
def test_once_retrieval_opened_only_a_break_out_of_its_consumption_window_moved(shift_s: float, moved: bool) -> None:
    """MediaTailor puts the retrieved ads in the first break of the consumption window with the schedule's event id
    (UG, "How prefetching works"): a break still inside the window (20 s before to 10 s after its end) keeps them."""
    first = brk(1, 8)
    shifted = Break(1, first.upid, first.start + timedelta(seconds=shift_s), first.duration_ms, first.program)
    retrieving = first.start - timedelta(seconds=100)  # retrieval opened 50 s ago, sessions retrieved
    result = plan([shifted], [existing(first)], configuration=SIBLING, policy=POLICY, now=retrieving, tags=TAGS)
    assert (result.delete == ["pf-1"]) is moved


def test_expired_and_foreign_schedules_are_left_alone() -> None:
    expired = existing(brk(9, -30))
    foreign = {"Name": "operator-test", "ScheduleType": "SINGLE", "Consumption": {"EndTime": at(30)}}
    result = plan([brk(1, 5)], [expired, foreign], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert result.delete == [] and [r["Name"] for r in result.create] == ["pf-1"]


def test_never_beside_a_recurring_schedule() -> None:
    recurring = {"Name": "legacy", "ScheduleType": "RECURRING"}
    result = plan([brk(1, 5)], [recurring], configuration=SIBLING, policy=POLICY, now=NOW, tags=TAGS)
    assert result.create == [] and result.delete == [] and result.refused is not None and "legacy" in result.refused


def test_the_quota_holds_while_every_schedule_is_still_wanted() -> None:
    breaks = [brk(n, 5 + 3 * n) for n in range(1, 30)]
    active = [existing(b) for b in breaks[: ACTIVE_LIMIT - 1]]
    result = plan(breaks, active, configuration=SIBLING, policy=PrefetchPolicy(horizon_breaks=24), now=NOW, tags=TAGS)
    assert (len(result.kept), result.create, result.delete) == (ACTIVE_LIMIT - 1, [], [])


def test_no_sibling_carries_yield_optimization() -> None:
    """Prefetch configurations never use YO (not supported with prefetch): the sibling model refuses it."""
    sibling = sibling_playback_configuration(
        name=SIBLING,
        video_content_source_url="https://ca.example/v1/channel/fastcif-demo/",
        content_base_url="https://cdn.example",
        pbs_origin="https://pbs.example",
        log_percent=100,
        function_mapping={},
    )
    document = sibling.api() | {"YieldOptimizationConfiguration": {"PublisherId": "aps", "Region": "us-east-1"}}
    with pytest.raises(ValidationError, match=r"yield_optimization_configuration|YieldOptimizationConfiguration"):
        PlaybackConfiguration.model_validate(document)
