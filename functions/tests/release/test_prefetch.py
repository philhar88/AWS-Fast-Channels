"""The prefetcher: the running monetized channels' schedules of record into each prefetching sibling's SINGLE
prefetch schedules (deleted before created), and nothing for a stopped channel."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from fast_channels.core.monetization.prefetch import PrefetchPolicy
from fast_channels.core.monetization.release import ChannelState, SiblingRecord
from fast_channels.core.timeline import upid
from fast_channels.functions.release import prefetch
from fast_channels.functions.settings import settings

type Stub = Callable[[str], Stubber]
NOW = datetime.now(UTC).replace(microsecond=0)
CHANNEL = "fastm-demo"
RECORD = SiblingRecord(release=2, digest="0" * 64, change_id="c2", at=datetime(2026, 10, 30, tzinfo=UTC))
RUNNING = {"ChannelState": "RUNNING", "LogConfiguration": {"LogTypes": []}}
STOPPED = {"ChannelState": "STOPPED", "LogConfiguration": {"LogTypes": []}}
PC_ARN = "arn:aws:mediatailor:us-east-1:123456789012:prefetchSchedule"
STATE = ChannelState(
    channel=CHANNEL,
    stable=f"{CHANNEL}-p-a",
    idle=f"{CHANNEL}-p-b",
    siblings={f"{CHANNEL}-p-{s}": RECORD for s in ("a", "b", "z")},
)


@pytest.fixture(autouse=True)
def prefetching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PREFETCH_SIBLINGS", "a,b")
    monkeypatch.setenv("BREAK_LENGTHS_MS", "30030,60060,90090,120120")
    monkeypatch.setenv("PREFETCH_POLICY", PrefetchPolicy(horizon_breaks=2).model_dump_json())
    settings.cache_clear()
    prefetch.policy.cache_clear()
    monkeypatch.setattr(prefetch, "states", lambda: [STATE])


def schedule(*starts: datetime) -> dict[str, Any]:
    return {
        "Items": [
            {
                "Arn": f"arn:aws:mediatailor:us-east-1:123456789012:program/{CHANNEL}/20261030T120000-sintel",
                "ChannelName": CHANNEL,
                "SourceLocationName": "fastm-media",
                "ProgramName": "20261030T120000-sintel",
                "ScheduleEntryType": "PROGRAM",
                "ApproximateStartTime": NOW,
                "ScheduleAdBreaks": [{"ApproximateDurationSeconds": 60, "ApproximateStartTime": s} for s in starts],
            }
        ]
    }


def program(*event_ids: int) -> dict[str, Any]:
    descriptor = {"SegmentationUpidType": 12, "SegmentationUpid": upid("sintel"), "SegmentationTypeId": 52}
    return {
        "ProgramName": "20261030T120000-sintel",
        "AdBreaks": [
            {
                "OffsetMillis": 100_000 * n,
                "MessageType": "TIME_SIGNAL",
                "TimeSignalMessage": {"SegmentationDescriptors": [descriptor | {"SegmentationEventId": e}]},
            }
            for n, e in enumerate(event_ids)
        ],
    }


def test_each_prefetching_sibling_gets_the_next_breaks(stub: Stub, context: Any) -> None:
    mediatailor = stub("mediatailor")
    starts = [NOW + timedelta(minutes=m) for m in (5, 9, 13)]
    mediatailor.add_response("describe_channel", RUNNING, {"ChannelName": CHANNEL})
    mediatailor.add_response(
        "get_channel_schedule", schedule(*starts), {"ChannelName": CHANNEL, "DurationMinutes": "360"}
    )
    mediatailor.add_response(
        "describe_program",
        program(11, 12, 13),
        {"ChannelName": CHANNEL, "ProgramName": "20261030T120000-sintel"},
    )
    stale = {
        "Arn": f"{PC_ARN}/{CHANNEL}-p-a/pf-99",
        "Name": "pf-99",
        "PlaybackConfigurationName": f"{CHANNEL}-p-a",
        "ScheduleType": "SINGLE",
        "Retrieval": {"EndTime": NOW + timedelta(minutes=2)},
        "Consumption": {"StartTime": NOW + timedelta(minutes=3), "EndTime": NOW + timedelta(minutes=5)},
    }
    for sibling, items in ((f"{CHANNEL}-p-a", [stale]), (f"{CHANNEL}-p-b", [])):
        mediatailor.add_response(
            "list_prefetch_schedules",
            {"Items": items},
            {"PlaybackConfigurationName": sibling, "ScheduleType": "ALL"},
        )
        if items:
            mediatailor.add_response(
                "delete_prefetch_schedule", {}, {"Name": "pf-99", "PlaybackConfigurationName": sibling}
            )
        for event_id in (11, 12):
            mediatailor.add_response(
                "create_prefetch_schedule",
                {"Name": f"pf-{event_id}", "Arn": f"{PC_ARN}/{sibling}/pf-{event_id}"},
                {
                    "Name": f"pf-{event_id}",
                    "PlaybackConfigurationName": sibling,
                    "ScheduleType": "SINGLE",
                    "Retrieval": ANY,
                    "Consumption": ANY,
                    "Tags": {"fast:app": "fastm", "fast:owner": "release-controller"},
                },
            )

    totals = prefetch.handler({"id": "event-1"}, context)

    assert totals == {"created": 4, "deleted": 1, "kept": 0, "refused": 0, "errors": 0}


def test_a_stopped_channel_gets_nothing(stub: Stub, context: Any) -> None:
    stub("mediatailor").add_response("describe_channel", STOPPED, {"ChannelName": CHANNEL})
    assert prefetch.handler({}, context)["created"] == 0


def test_a_recurring_schedule_refuses_the_sibling_and_a_failure_does_not_stop_the_others(
    stub: Stub, context: Any
) -> None:
    mediatailor = stub("mediatailor")
    mediatailor.add_response("describe_channel", RUNNING, {"ChannelName": CHANNEL})
    mediatailor.add_response("get_channel_schedule", {"Items": []}, {"ChannelName": CHANNEL, "DurationMinutes": "360"})
    mediatailor.add_response(
        "list_prefetch_schedules",
        {
            "Items": [
                {
                    "Arn": f"{PC_ARN}/{CHANNEL}-p-a/legacy",
                    "Name": "legacy",
                    "PlaybackConfigurationName": f"{CHANNEL}-p-a",
                    "ScheduleType": "RECURRING",
                }
            ]
        },
        {"PlaybackConfigurationName": f"{CHANNEL}-p-a", "ScheduleType": "ALL"},
    )
    mediatailor.add_client_error("list_prefetch_schedules", service_error_code="TooManyRequestsException")

    totals = prefetch.handler({}, context)

    assert (totals["refused"], totals["errors"]) == (1, 1)


def test_only_the_configured_letters() -> None:
    assert prefetch.siblings_to_schedule(CHANNEL, sorted(STATE.siblings)) == [f"{CHANNEL}-p-a", f"{CHANNEL}-p-b"]


def test_a_run_over_an_unchanged_schedule_makes_no_write(stub: Stub, context: Any) -> None:
    """The owner's direction (2026-10-30): the schedule of record decides, and a run over an unchanged one is a no-op.
    Both siblings already have their breaks' schedules (in whole seconds, as MediaTailor keeps them) and Channel
    Assembly's approximate starts moved by 1.3 s: no Create or DeletePrefetchSchedule is made (the stubber would fail
    on one)."""
    mediatailor = stub("mediatailor")
    starts = [NOW + timedelta(minutes=m, milliseconds=700) for m in (5, 9)]
    jittered = [s + timedelta(milliseconds=1300) for s in starts]
    mediatailor.add_response("describe_channel", RUNNING, {"ChannelName": CHANNEL})
    mediatailor.add_response(
        "get_channel_schedule", schedule(*jittered), {"ChannelName": CHANNEL, "DurationMinutes": "360"}
    )
    mediatailor.add_response(
        "describe_program", program(11, 12), {"ChannelName": CHANNEL, "ProgramName": "20261030T120000-sintel"}
    )
    for sibling in (f"{CHANNEL}-p-a", f"{CHANNEL}-p-b"):
        items = [
            {
                "Arn": f"{PC_ARN}/{sibling}/pf-{event_id}",
                "Name": f"pf-{event_id}",
                "PlaybackConfigurationName": sibling,
                "ScheduleType": "SINGLE",
                "Retrieval": {
                    "StartTime": (start - timedelta(seconds=150)).replace(microsecond=0),
                    "EndTime": (start - timedelta(seconds=30)).replace(microsecond=0),
                },
                "Consumption": {
                    "StartTime": (start - timedelta(seconds=20)).replace(microsecond=0),
                    "EndTime": (start + timedelta(seconds=70)).replace(microsecond=0),
                },
            }
            for event_id, start in zip((11, 12), starts, strict=True)
        ]
        mediatailor.add_response(
            "list_prefetch_schedules", {"Items": items}, {"PlaybackConfigurationName": sibling, "ScheduleType": "ALL"}
        )

    totals = prefetch.handler({"id": "event-2"}, context)

    assert totals == {"created": 0, "deleted": 0, "kept": 4, "refused": 0, "errors": 0}
