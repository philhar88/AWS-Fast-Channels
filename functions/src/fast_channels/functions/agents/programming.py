"""The programming agent's trigger (M9, runbook R6): a programmed channel whose schedule runs out within 6 hours.

An hourly rule starts the programming agent's runner. The runner first reads, deterministically, where each of the
envelope's programmed channels' schedule ends (Channel Assembly's ``GetChannelSchedule``, programs only): when every
one reaches 6 hours ahead there is nothing to do and no conversation. Otherwise the agent is asked to extend the
short ones with blocks that start at least 2 hours ahead, the envelope's publish lead; a nearer gap is left for a
human (an edit under 2 hours is L3), and its publish would be refused by the runner and by the Gateway. After the
run the runner reads the runway again: a channel still short (no asset the agent would air, a plan it could not
publish) is escalated to a human.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import BotoCoreError, ClientError

from fast_channels.functions import aws

if TYPE_CHECKING:
    from fast_channels.core.ledger import Json

SCHEDULED: Final = "Scheduled Event"
PUBLISH: Final = "schedule___publish"
RUNWAY: Final = timedelta(hours=6)
"""R6: a schedule that ends sooner than this is extended."""
HORIZON: Final = timedelta(hours=8)
"""How far ahead the agent is asked to extend a short schedule: past :data:`RUNWAY`, so the next tick has nothing to
do."""
LOOK_AHEAD_MINUTES: Final = 24 * 60
MARGIN: Final = timedelta(minutes=10)
"""Kept between the publish lead and the earliest start the agent is asked for (planning takes minutes)."""


def schedule_end(channel: str, now: datetime) -> datetime | None:
    """When ``channel``'s last scheduled program within a day ends (None: no program after ``now``).

    Raises:
        ClientError, BotoCoreError: the schedule could not be read.
    """
    end: datetime | None = None
    paginator = aws.client("mediatailor").get_paginator("get_channel_schedule")
    for page in paginator.paginate(ChannelName=channel, DurationMinutes=str(LOOK_AHEAD_MINUTES)):
        for item in page.get("Items", []):
            if item.get("ScheduleEntryType", "PROGRAM") != "PROGRAM":
                continue
            ends = item["ApproximateStartTime"] + timedelta(seconds=int(item.get("ApproximateDurationSeconds", 0)))
            if ends > now and (end is None or ends > end):
                end = ends
    return end


def runways(channels: Sequence[str], now: datetime) -> dict[str, dict[str, Json]]:
    """Per programmed channel: where its schedule ends and its runway in minutes, or the error that hid them."""
    found: dict[str, dict[str, Json]] = {}
    for channel in channels:
        try:
            end = schedule_end(channel, now)
        except (ClientError, BotoCoreError) as error:
            code = error.response.get("Error", {}).get("Code") if isinstance(error, ClientError) else None
            found[channel] = {"error": str(code or type(error).__name__)}
            continue
        minutes = 0 if end is None else int((end - now).total_seconds() // 60)
        found[channel] = {"ends": None if end is None else _iso(end), "runway_minutes": minutes}
    return found


def short(found: Mapping[str, Mapping[str, Json]]) -> list[str]:
    """The channels whose runway is under :data:`RUNWAY`."""
    limit = RUNWAY.total_seconds() // 60
    return [
        channel
        for channel, facts in found.items()
        if isinstance(minutes := facts.get("runway_minutes"), int) and minutes < limit
    ]


def still_short(channels: Sequence[str], now: datetime) -> list[str]:
    """After a run: each of ``channels`` whose runway is still under :data:`RUNWAY`, as a sentence for a human."""
    found = runways(channels, now)
    return [
        f"{channel}: its schedule still ends {found[channel].get('runway_minutes')} minutes ahead "
        f"(runbook R6 needs {int(RUNWAY.total_seconds() // 60)})"
        for channel in short(found)
    ]


def is_tick(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` is the hourly rule's (an EventBridge scheduled event)."""
    return event.get("detail-type") == SCHEDULED and event.get("source") == "aws.events"


def prompt(found: Mapping[str, Mapping[str, Json]], channels: Sequence[str], now: datetime, lead: timedelta) -> str:
    """What the programming agent is asked: facts, then the one thing to do."""
    earliest = now + lead + MARGIN
    parts = []
    for channel in channels:
        facts = found[channel]
        ends = facts.get("ends")
        appends = isinstance(ends, str) and datetime.fromisoformat(ends) >= earliest
        first = (
            "append its first block to the published schedule (omit start)"
            if appends
            else f"start its first block at {_iso(earliest)} (pass it as start)"
        )
        seen = f"ends at {ends}" if isinstance(ends, str) else "has nothing scheduled"
        parts.append(
            f"- {channel}: its schedule {seen}, {facts.get('runway_minutes')} minutes ahead. {first.capitalize()}, "
            f"then append blocks until it reaches at least {_iso(now + HORIZON)}."
        )
    return (
        "You are the programming agent of this FAST Channels deployment. These channels' schedules run out within "
        f"{int(RUNWAY.total_seconds() // 3600)} hours (runbook R6):\n" + "\n".join(parts) + "\n\n"
        "For each channel: read its brief (schedule___get_brief) and its schedule (schedule___get_schedule), find "
        "READY assets of its ladder (library___find_assets), plan blocks with schedule___plan from that start, and "
        "publish every plan that has no error violations with schedule___publish. Your envelope publishes only "
        f"plans that start at least {int(lead.total_seconds() // 60)} minutes from now: leave anything nearer, and "
        "every other change, to a human.\n\n"
        f"Facts: {json.dumps({'now': _iso(now), 'channels': dict(found)}, sort_keys=True)}"
    )


def _iso(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
