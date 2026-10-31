"""The Release Controller's prefetcher (M3-06, DESIGN §10): keeps each running monetized channel's prefetching
siblings scheduled for their next breaks (``core.monetization.prefetch``).

EventBridge Scheduler runs it every few minutes, and the stage's bus on ``Schedule Published`` (a plan just changed
the schedule) and ``Census Changed`` (a channel started). For each channel the module monetizes that is RUNNING:

1. ``GetChannelSchedule`` for the next hours, and ``DescribeProgram`` for each program with breaks;
2. for each sibling the controller recorded whose letter is in ``modules.release.prefetch.siblings``,
   ``ListPrefetchSchedules`` (``ScheduleType=ALL``: RECURRING ones are hidden otherwise, P0-02);
3. the plan: ``DeletePrefetchSchedule`` first (re-planned or moved breaks), then ``CreatePrefetchSchedule``.

A stopped channel gets nothing: no session retrieves, and its schedule moves when it starts. Failures on one sibling
do not stop the others; they are counted in ``PrefetchErrors``, whose alarm notifies the alert topic.
"""

from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from fast_channels.core.monetization.prefetch import (
    Plan,
    PrefetchPolicy,
    breaks_of,
    plan,
    schedule_tags,
)
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import ignore_missing
from fast_channels.functions.release.drift import states
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

    from fast_channels.core.monetization.prefetch import Break

SCHEDULE_MINUTES: Final = 360
"""How far ahead the schedule of record is read: more than ``horizon_breaks`` breaks of any FAST channel."""

logger = Logger()
metrics = Metrics()


@cache
def policy() -> PrefetchPolicy:
    """The stage's prefetch timing (``PREFETCH_POLICY``)."""
    raw = settings().prefetch_policy
    return PrefetchPolicy.model_validate_json(raw) if raw else PrefetchPolicy()


def running(channel: str) -> bool:
    """Whether ``channel`` is RUNNING (a missing channel is not)."""
    try:
        state = aws.client("mediatailor").describe_channel(ChannelName=channel)["ChannelState"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NotFoundException", "ResourceNotFoundException"}:
            return False
        raise
    return bool(state == "RUNNING")


def channel_breaks(channel: str) -> list[Break]:
    """The channel's breaks of the next :data:`SCHEDULE_MINUTES`."""
    mediatailor = aws.client("mediatailor")
    entries = [
        item
        for page in mediatailor.get_paginator("get_channel_schedule").paginate(
            ChannelName=channel, DurationMinutes=str(SCHEDULE_MINUTES)
        )
        for item in page.get("Items", [])
    ]
    programs = {
        name: mediatailor.describe_program(ChannelName=channel, ProgramName=name)
        for name in sorted({e["ProgramName"] for e in entries if e.get("ScheduleAdBreaks")})
    }
    breaks, skipped = breaks_of(entries, programs, breaks_ms=settings().break_lengths_ms)
    if skipped:
        logger.warning("breaks left to real-time insertion", extra={"channel": channel, "skipped": skipped[:20]})
    return breaks


def existing(configuration: str) -> list[dict[str, Any]]:
    """Every prefetch schedule of ``configuration``, SINGLE and RECURRING."""
    return [
        item
        for page in aws.client("mediatailor")
        .get_paginator("list_prefetch_schedules")
        .paginate(PlaybackConfigurationName=configuration, ScheduleType="ALL")
        for item in page.get("Items", [])
    ]


def carry_out(result: Plan) -> dict[str, int]:
    """Delete, then create, what ``result`` names."""
    mediatailor = aws.client("mediatailor")
    for name in result.delete:
        ignore_missing(mediatailor.delete_prefetch_schedule, Name=name, PlaybackConfigurationName=result.configuration)
    for request in result.create:
        mediatailor.create_prefetch_schedule(**request)
    return {"created": len(result.create), "deleted": len(result.delete), "kept": len(result.kept)}


def siblings_to_schedule(channel: str, recorded: list[str]) -> list[str]:
    """The recorded siblings of ``channel`` whose letter prefetches."""
    letters = set(settings().prefetch_letters)
    return sorted(name for name in recorded if name.removeprefix(f"{channel}-p-") in letters)


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Bring every prefetching sibling of the running monetized channels in line with its channel's schedule."""
    logger.set_correlation_id(event.get("id") or event.get("scheduled_time"))
    config = settings()
    config.require("prefix")
    now = datetime.now(UTC)
    totals = {"created": 0, "deleted": 0, "kept": 0, "refused": 0, "errors": 0}
    for state in states():
        if not (configurations := siblings_to_schedule(state.channel, sorted(state.siblings))):
            continue
        if not running(state.channel):
            logger.info("channel not running: no prefetch", extra={"channel": state.channel})
            continue
        breaks = channel_breaks(state.channel)
        for configuration in configurations:
            try:
                result = plan(
                    breaks,
                    existing(configuration),
                    configuration=configuration,
                    policy=policy(),
                    now=now,
                    tags=schedule_tags(config.prefix),
                )
                if result.refused:
                    logger.error("prefetch refused", extra={"configuration": configuration, "why": result.refused})
                    totals["refused"] += 1
                    continue
                counts = carry_out(result)
            except ClientError:
                logger.exception("prefetch failed", extra={"configuration": configuration})
                totals["errors"] += 1
                continue
            for key, value in counts.items():
                totals[key] += value
            logger.info(
                "prefetch schedules",
                extra={
                    "configuration": configuration,
                    "counts": counts,  # not top-level: "created" is a LogRecord attribute
                    "created_names": [r["Name"] for r in result.create],
                    "deleted_names": result.delete,
                    "left_out": result.left_out[:20],
                },
            )
    for name, key in (
        ("PrefetchSchedulesCreated", "created"),
        ("PrefetchSchedulesDeleted", "deleted"),
        ("PrefetchRefused", "refused"),
        ("PrefetchErrors", "errors"),
    ):
        metrics.add_metric(name=name, unit=MetricUnit.Count, value=totals[key])
    return totals
