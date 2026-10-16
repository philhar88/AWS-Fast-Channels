"""A channel started after its block began: the programs whose start has passed are skipped, and every later program
airs at its planned time (docs/channel-assembly.md, "Starting a channel late").

Runs after ``test_06_stop``, with the channel stopped. A two-program block is published to start in a few minutes,
the channel is started 30 s after the first program's start, and Channel Assembly's as-run log must show the second
program starting at its planned time (within one segment) and nothing of the first. The channel is stopped at the
end of the module.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import pytest
from boto3.dynamodb.conditions import Key
from harness import until, wait_for

from fast_channels.functions.tools.library_store import GSI1, ProgramRecord, ProgramStatus

if TYPE_CHECKING:
    from _stack import Stage
    from harness import Channel, Tools
    from mypy_boto3_dynamodb.service_resource import Table

    from fast_channels.core.ladder import Ladder
    from fast_channels.functions.tools.library_store import Asset

pytestmark = pytest.mark.e2e

AS_RUN_LOG_GROUP: Final = "MediaTailor/Channel/AsRunLog"
"""Channel Assembly's as-run log group (fixed name, shared by the account's channels in the region)."""
LEAD: Final = timedelta(minutes=3)
LATE_BY: Final = timedelta(seconds=30)
BREAK_POLICY: Final = {"cadence_minutes": 0.5, "min_gap_minutes": 0.25, "max_breaks": 1, "slate_gops": 60}
"""One 2-minute break per program at the test pattern's cue point: the first program airs for about 3 minutes, so
the channel is running well before the second one is due."""
AS_RUN_DELAY: Final = timedelta(minutes=3)
"""How long after its start a program's as-run event may take to be readable."""


@dataclass(frozen=True, slots=True)
class LateStart:
    first: dict[str, Any]
    second: dict[str, Any]
    started: dict[str, Any]
    """The ``channels___start_channel`` result."""
    started_at: datetime


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


@pytest.fixture(scope="module")
def late_start(tools: Tools, channel: Channel, ingested: Asset) -> Iterator[LateStart]:
    """The block published while the channel is stopped, and the channel started 30 s into its first program."""
    tools("channels___stop_channel", channel=channel.name)  # stopped by test_06 already; a no-op then
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel.name,
        picks=[ingested.asset_id] * 2,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], plan["violations"]
    first, second = tools("schedule___publish", plan_id=plan["plan_id"])["programs"]
    until(_at(first["start"]) + LATE_BY)
    try:
        started_at = datetime.now(UTC)
        yield LateStart(first, second, tools("channels___start_channel", channel=channel.name), started_at)
    finally:
        tools("channels___stop_channel", channel=channel.name)


def as_run_starts(stage: Stage, channel: str, since: datetime) -> dict[str, datetime]:
    """When each program of ``channel`` first started on air after ``since`` (any output), from the as-run log."""
    logs = stage.session.client("logs", region_name=stage.region)
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=AS_RUN_LOG_GROUP,
        logStreamNamePrefix=f"{channel}-",  # one stream per output: <channel>-<source group>
        startTime=int(since.timestamp() * 1000),
        filterPattern=f'{{ $.channelName = "{channel}" && $.eventType = "VodProgramStart" }}',
    )
    starts: dict[str, datetime] = {}
    for page in pages:
        for event in page.get("events", []):
            entry = json.loads(event["message"])
            at = _at(entry["timeStamp"])
            starts[entry["programName"]] = min(at, starts.get(entry["programName"], at))
    return starts


def test_the_missed_program_is_reported_skipped(late_start: LateStart) -> None:
    started = late_start.started
    assert started["state"] == "RUNNING"
    skipped = {p["name"]: p for p in started.get("skipped", [])}
    assert late_start.first["name"] in skipped and late_start.second["name"] not in skipped, started
    assert skipped[late_start.first["name"]]["start"] == late_start.first["start"]
    assert all(_at(p["start"]) < late_start.started_at for p in skipped.values()), skipped  # never a later one
    assert late_start.first["title"] in started["hint"]


def test_the_guide_marks_it_skipped(library: Table, late_start: LateStart) -> None:
    key = Key("gsi1pk").eq(f"PROGRAM#{late_start.first['name']}")
    [item] = library.query(IndexName=GSI1, KeyConditionExpression=key)["Items"]
    assert ProgramRecord.from_item(item).status is ProgramStatus.SKIPPED


def test_the_next_program_airs_at_its_planned_time(
    stage: Stage, channel: Channel, ladder: Ladder, late_start: LateStart
) -> None:
    first, second = late_start.first, late_start.second
    planned = _at(second["start"])
    seen: dict[str, datetime] = {}

    def aired() -> datetime | None:
        seen.update(as_run_starts(stage, channel.name, late_start.started_at - timedelta(seconds=5)))
        return seen.get(second["name"])

    timeout = (planned + AS_RUN_DELAY - datetime.now(UTC)).total_seconds()
    at = wait_for(
        aired, timeout=max(timeout, 60), interval=15, what=f"{second['name']} in the as-run log", state=lambda: seen
    )
    segment = timedelta(milliseconds=ladder.grid.segment_ms)
    assert abs(at - planned) <= segment, f"{second['name']} aired at {at:%H:%M:%S.%f}, planned {planned:%H:%M:%S.%f}"
    assert first["name"] not in seen, seen
