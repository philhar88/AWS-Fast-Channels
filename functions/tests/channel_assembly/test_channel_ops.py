import time
from collections.abc import Callable
from typing import Any

import pytest
from boto3.dynamodb.conditions import Key
from botocore.stub import ANY, Stubber

from fast_channels.functions import channel_ops
from fast_channels.functions.media_library import program_sk

type Stub = Callable[[str], Stubber]
type Typed = Callable[[dict[str, Any]], dict[str, Any]]

CHANNEL = "fast-agent-news"
NOW = 1_792_000_000_000
HOUR, DAY = 3_600_000, 86_400_000
SCHEDULE = {"ChannelName": CHANNEL, "DurationMinutes": "20160"}


def program(name: str, status: str, start_ms: int, duration_ms: int = 600_000) -> dict[str, Any]:
    return {"name": name, "status": status, "start_ms": start_ms, "duration_ms": duration_ms}


def library_query(start_ms: int) -> dict[str, Any]:
    return {
        "TableName": "library",
        "KeyConditionExpression": Key("pk").eq(f"CHANNEL#{CHANNEL}")
        & Key("sk").between(program_sk(start_ms), program_sk(10**13 - 1)),
        "ProjectionExpression": "#n, #s, start_ms, duration_ms",
        "ExpressionAttributeNames": {"#n": "name", "#s": "status"},
    }


def stopped(stub: Stub) -> None:
    stub("mediatailor").add_response("stop_channel", {}, {"ChannelName": CHANNEL})
    stub("mediatailor").add_response(
        "describe_channel", {"ChannelState": "STOPPED", "LogConfiguration": {}}, {"ChannelName": CHANNEL}
    )


def entry(name: str) -> dict[str, Any]:
    return {
        "Arn": "arn",
        "ChannelName": CHANNEL,
        "ProgramName": name,
        "SourceLocationName": "fast-media",
        "ScheduleEntryType": "PROGRAM",
        "ApproximateDurationSeconds": 60,
    }


def test_drain_skips_the_epg_history_that_channel_assembly_has_deleted(
    stub: Stub, typed: Typed, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A channel that ran around the clock: thousands of aired programs in the last day alone, and only the key
    # range from a day ago is read at all (older history never is).
    monkeypatch.setattr(time, "time_ns", lambda: NOW * 1_000_000)
    aired = [program(f"aired-{n:04d}", "AIRED", NOW - DAY + n * 1_000, duration_ms=1_000) for n in range(3_000)]
    held = [
        program("aired-just-now", "AIRED", NOW - 20 * 60_000),  # ended 10 minutes ago: maybe not deleted yet
        program("in-three-weeks", "PUBLISHED", NOW + 21 * DAY),  # beyond the schedule's 14-day look-ahead
        program("never-aired", "PUBLISHED", NOW - 20 * HOUR),  # published while the channel was stopped
        program("tomorrow", "PUBLISHED", NOW + DAY),
    ]
    stopped(stub)
    stub("mediatailor").add_response("get_channel_schedule", {"Items": [entry("tomorrow")]}, SCHEDULE)
    stub("dynamodb").add_response("query", {"Items": [typed(i) for i in [*aired, *held]]}, library_query(NOW - DAY))
    for name in sorted(p["name"] for p in held):
        stub("mediatailor").add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": name})

    drained = channel_ops.drain_channel(CHANNEL, table_name="library")

    assert drained == channel_ops.Drained(deleted=4, remaining=0)


def test_drain_deletes_the_schedule_and_the_recent_library_programs(stub: Stub, typed: Typed) -> None:
    stopped(stub)
    stub("mediatailor").add_response("get_channel_schedule", {"Items": [entry("p1"), entry("p2")]}, SCHEDULE)
    recent = [typed(program("p2", "PUBLISHED", NOW)), typed(program("p3", "PUBLISHED", NOW))]
    stub("dynamodb").add_response("query", {"Items": recent}, {**library_query(0), "KeyConditionExpression": ANY})
    for name in ("p1", "p2", "p3"):
        stub("mediatailor").add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": name})

    drained = channel_ops.drain_channel(CHANNEL, table_name="library")

    assert drained == channel_ops.Drained(deleted=3, remaining=0)
    assert drained.complete


def test_drain_stops_when_out_of_time(stub: Stub) -> None:
    stopped(stub)
    stub("mediatailor").add_response("get_channel_schedule", {"Items": [entry("p1"), entry("p2")]}, SCHEDULE)
    stub("mediatailor").add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p1"})
    checks = iter([False, True])

    drained = channel_ops.drain_channel(CHANNEL, out_of_time=lambda: next(checks))

    assert drained == channel_ops.Drained(deleted=1, remaining=1)
    assert not drained.complete


def test_drain_of_a_missing_channel_does_nothing(stub: Stub, client_error: Callable[..., dict[str, Any]]) -> None:
    stub("mediatailor").add_client_error("stop_channel", **client_error("NotFoundException", status=404))

    assert channel_ops.drain_channel(CHANNEL, table_name="library") == channel_ops.Drained(deleted=0, remaining=0)


def create_request(name: str) -> dict[str, Any]:
    return {
        "ChannelName": CHANNEL,
        "ProgramName": name,
        "SourceLocationName": "fast-media",
        "VodSourceName": "v",
        "ScheduleConfiguration": {"Transition": {"Type": "RELATIVE", "RelativePosition": "AFTER_PROGRAM"}},
    }


def test_create_programs_yields_each_program_once_it_exists(
    stub: Stub, client_error: Callable[..., dict[str, Any]]
) -> None:
    mediatailor = stub("mediatailor")
    requests = [create_request(name) for name in ("p1", "p2", "p3")]
    mediatailor.add_response("create_program", {}, requests[0])
    mediatailor.add_client_error("create_program", **client_error("ConflictException", "The schedule has changed"))
    mediatailor.add_response("create_program", {}, requests[1])
    mediatailor.add_client_error(
        "create_program", **client_error("BadRequestException", "This program already exists.")
    )

    assert list(channel_ops.create_programs(requests)) == [("p1", True), ("p2", True), ("p3", False)]


def test_create_programs_stops_at_the_first_failure(stub: Stub, client_error: Callable[..., dict[str, Any]]) -> None:
    mediatailor = stub("mediatailor")
    requests = [create_request(name) for name in ("p1", "p2", "p3")]
    mediatailor.add_response("create_program", {}, requests[0])
    mediatailor.add_client_error("create_program", **client_error("BadRequestException", "VOD source missing"))
    created = []

    with pytest.raises(Exception, match="VOD source missing"):
        for name, _ in channel_ops.create_programs(requests):
            created.append(name)

    assert created == ["p1"]  # the caller has recorded p1 before the failure


def test_delete_programs_retries_conflicts(stub: Stub, client_error: Callable[..., dict[str, Any]]) -> None:
    mediatailor = stub("mediatailor")
    mediatailor.add_client_error("delete_program", **client_error("ConflictException"))
    mediatailor.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p1"})
    mediatailor.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p2"})

    assert channel_ops.delete_programs(CHANNEL, ["p1", "p2"]) == ["p1", "p2"]
