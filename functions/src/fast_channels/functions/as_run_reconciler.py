"""Mark scheduled programs ``AIRED`` from Channel Assembly's as-run log, and with the ops module their breaks too.

``AsRunLogging`` subscribes this function to ``MediaTailor/Channel/AsRunLog`` with a filter for ``VodProgramStart``
events of the deployment's channels (and ``AdBreakStart`` events with the ops module on, which also sets
``AS_RUN_BREAKS``; without it break starts are ignored, as in v3.0). Channel Assembly writes
one event per program start and output, e.g. ``{"channelName": "fast-demo", "eventType": "VodProgramStart",
"programName": "20261016T120000-bbb-1a2b3c", "sourceGroup": "hls", "timeStamp": "2026-10-16T12:00:03.170Z", ...}``
(channel-assembly.md §7.2), so the first event of a start updates the program's ``PROGRAM`` item and its duplicates
(other outputs, redeliveries) are no-ops. Programs the media library does not know (published outside the schedule
tools) are ignored.

An ``AdBreakStart`` has no break id: :func:`mark_break_aired` finds the program's break whose on-air time is nearest
(``core.as_run.matching_break``, from the program's ``aired_at`` or else its planned start) and sets that break's
``aired_at`` and ``status`` ``AIRED`` on the ``PROGRAM`` item, once. :func:`sweep` (scheduled) marks the breaks
whose time has long passed without a start ``UNAIRED``: Channel Assembly logs nothing for a break it skips, for
example after the channel stopped. A break that went on air up to a minute before a ``StopChannel`` of its channel
aired, but its ``AdBreakStart`` can be lost (W-2 1.31): the sweep reads the channels' stops from CloudTrail's event
history (:func:`channel_stops`) and marks such a break ``AIRED`` at its planned on-air time, with the stop's
``stopped_at``.
"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudWatchLogsEvent
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fast_channels.core.as_run import STOP_LOSS_MS, break_air_times, matching_break, stopped_after, unaired_breaks
from fast_channels.core.timeline import PlannedBreak
from fast_channels.functions import aws
from fast_channels.functions.media_library import (
    CHANNEL_PREFIX,
    GSI1,
    ProgramStatus,
    iso,
    now_iso,
    program_index,
    program_sk,
)
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from aws_lambda_powertools.utilities.typing import LambdaContext

PROGRAM_START: Final = "VodProgramStart"
BREAK_START: Final = "AdBreakStart"
SWEEP_WINDOW_MS: Final = 26 * 3600_000
"""Programs that started this long ago have no break left to decide (the longest program is far shorter)."""
HISTORY_LAG_MS: Final = 15 * 60_000
"""How long a ``StopChannel`` call may take to reach CloudTrail's event history (about 5 minutes on average, not
guaranteed): a break that went on air less than this plus ``STOP_LOSS_MS`` ago waits for a later sweep."""
STOP_SOURCE: Final = "mediatailor.amazonaws.com"
"""``eventSource`` of Channel Assembly's ``StopChannel`` (AWS Elemental MediaLive has an action of that name too)."""
DENIED: Final = frozenset({"AccessDenied", "AccessDeniedException"})


class BreakStatus(StrEnum):
    """``status`` of a break on its ``PROGRAM`` item (ops module); absent until decided."""

    AIRED = "AIRED"
    UNAIRED = "UNAIRED"


logger = Logger()
metrics = Metrics()


class AsRunEvent(BaseModel):
    """The fields of an as-run log event that identify a program start."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    channel: str = Field(alias="channelName")
    event_type: str = Field(alias="eventType")
    program: str | None = Field(None, alias="programName")
    timestamp: str = Field(alias="timeStamp", description="ISO-8601 UTC time the program started on air.")


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> None:
    """Mark the program of every ``VodProgramStart`` event (and, with ``AS_RUN_BREAKS``, the break of every
    ``AdBreakStart`` event) aired. ``event`` is a CloudWatch Logs subscription batch, or one EventBridge event whose
    ``detail`` is an as-run event (an account's shared as-run router puts those)."""
    settings().require("library_table")
    aired = breaks = 0
    for start in _as_run_events(event):
        if not start.program:
            continue
        if start.event_type == PROGRAM_START and mark_aired(start):
            aired += 1
        elif start.event_type == BREAK_START and settings().as_run_breaks and mark_break_aired(start):
            breaks += 1
    metrics.add_metric(name="ProgramsAired", unit=MetricUnit.Count, value=aired)
    if breaks:
        metrics.add_metric(name="BreaksAired", unit=MetricUnit.Count, value=breaks)


def _as_run_events(event: dict[str, Any]) -> list[AsRunEvent]:
    """The as-run events of a subscription batch or of an EventBridge event; anything else is logged and dropped."""
    if "awslogs" in event:
        data = CloudWatchLogsEvent(event).parse_logs_data()
        logger.append_keys(log_stream=data.log_stream)
        messages = [log_event.message for log_event in data.log_events]
    elif isinstance(event.get("detail"), dict):
        logger.append_keys(bus_event_id=event.get("id"), as_run_id=event["detail"].get("as_run_id"))
        messages = [json.dumps(event["detail"])]
    else:
        logger.warning("neither a log subscription batch nor an EventBridge event", extra={"keys": sorted(event)})
        return []
    events = []
    for message in messages:
        try:
            events.append(AsRunEvent.model_validate_json(message))
        except ValidationError:
            logger.warning("not an as-run event", extra={"log_message": message[:500]})
    return events


def _program(start: AsRunEvent) -> dict[str, Any] | None:
    """The ``PROGRAM`` item of the event's program, or ``None`` (logged) if the media library does not know it."""
    index = program_index(str(start.program), start.channel)
    items = (
        aws.table(settings().library_table)
        .query(
            IndexName=GSI1,
            KeyConditionExpression=Key("gsi1pk").eq(index["gsi1pk"]) & Key("gsi1sk").eq(index["gsi1sk"]),
        )
        .get("Items", [])
    )
    if not items:
        logger.info("program not in the media library", extra={"channel": start.channel, "program": start.program})
        return None
    item = dict(items[0])  # program names are unique per channel (Channel Assembly rejects duplicates)
    logger.set_correlation_id(_text(item.get("correlation_id")))
    return item


def mark_aired(start: AsRunEvent) -> bool:
    """Set the program's status to AIRED (once); ``False`` if unknown or already aired."""
    table = aws.table(settings().library_table)
    item = _program(start)
    if item is None:
        return False
    try:
        table.update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression="SET #status = :aired, aired_at = :at, updated_at = :now",
            ConditionExpression="attribute_exists(pk) AND #status <> :aired",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":aired": ProgramStatus.AIRED.value, ":at": start.timestamp, ":now": now_iso()},
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    logger.info("program aired", extra={"channel": start.channel, "program": start.program, "at": start.timestamp})
    return True


def _start_ms(item: Mapping[str, Any]) -> int:
    """When the program went on air (its ``aired_at``), else its planned start."""
    if isinstance(aired := item.get("aired_at"), str):
        return _ms(aired)
    return int(item["start_ms"])


def _ms(iso: str) -> int:
    return round(datetime.fromisoformat(iso).timestamp() * 1000)


def _now_ms() -> int:
    return round(datetime.now(UTC).timestamp() * 1000)


def _breaks(item: Mapping[str, Any]) -> list[PlannedBreak]:
    return [PlannedBreak.model_validate(b) for b in item.get("breaks") or []]


def mark_break_aired(start: AsRunEvent) -> bool:
    """Set ``aired_at`` and status ``AIRED`` on the program's break that this ``AdBreakStart`` is (once); ``False``
    for an unknown program, a time near none of its breaks, or a break already marked."""
    item = _program(start)
    if item is None:
        return False
    breaks = _breaks(item)
    index = matching_break(_ms(start.timestamp), _start_ms(item), breaks)
    if index is None:
        logger.warning(
            "an ad break started at no scheduled break's time",
            extra={"channel": start.channel, "program": start.program, "at": start.timestamp},
        )
        return False
    if not _decide(item, index, BreakStatus.AIRED, aired_at=start.timestamp):
        return False
    logger.info(
        "ad break aired",
        extra={
            "channel": start.channel,
            "program": start.program,
            "break": index,
            "event_id": breaks[index].event_id,
            "at": start.timestamp,
        },
    )
    return True


def _decide(
    item: Mapping[str, Any],
    index: int,
    status: BreakStatus,
    *,
    aired_at: str | None = None,
    stopped_at: str | None = None,
) -> bool:
    """Set break ``index``'s status (and ``aired_at``, and ``stopped_at`` for a break the channel stopped in) on the
    program item, unless it is decided already."""
    names = {"#breaks": "breaks", "#status": "status"}
    values: dict[str, str] = {":status": status.value}
    update = f"SET #breaks[{index}].#status = :status"
    if aired_at is not None:
        update += f", #breaks[{index}].aired_at = :at"
        values[":at"] = aired_at
    if stopped_at is not None:
        update += f", #breaks[{index}].stopped_at = :stopped"
        values[":stopped"] = stopped_at
    try:
        aws.table(settings().library_table).update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression=update,
            ConditionExpression=f"attribute_exists(pk) AND attribute_not_exists(#breaks[{index}].#status)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    return True


@metrics.log_metrics
@logger.inject_lambda_context
def sweep(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Mark ``UNAIRED`` every break of the deployment's recent programs whose time passed long ago without an
    ``AdBreakStart`` (``core.as_run.unaired_breaks``); skipped programs' breaks never air either. A break that went
    on air shortly before a stop of its channel is marked ``AIRED`` instead (``core.as_run.stopped_after``)."""
    settings().require("library_table")
    now_ms = _now_ms()
    due: list[tuple[dict[str, Any], list[PlannedBreak], list[int], bool]] = []
    for item in _recent_programs(now_ms - SWEEP_WINDOW_MS):
        breaks = _breaks(item)
        decided = {i for i, b in enumerate(item.get("breaks") or []) if b.get("status")}
        if skipped := item.get("status") == ProgramStatus.SKIPPED.value:
            pending = [i for i in range(len(breaks)) if i not in decided]
        else:
            times = break_air_times(_start_ms(item), breaks)
            pending = [
                i
                for i in unaired_breaks(now_ms, _start_ms(item), breaks, decided)
                if times[i] + STOP_LOSS_MS + HISTORY_LAG_MS < now_ms
            ]
        if pending:
            due.append((item, breaks, pending, skipped))
    stops = _stops_around([(item, breaks, pending) for item, breaks, pending, skipped in due if not skipped])
    unaired = aired = 0
    for item, breaks, pending, skipped in due:
        cut = {} if skipped else stopped_after(_start_ms(item), breaks, pending, stops.get(_channel(item), []))
        for index in pending:
            event = {"program": item.get("name"), "break": index, "event_id": breaks[index].event_id}
            if (stop := cut.get(index)) is not None:
                on_air = iso(break_air_times(_start_ms(item), breaks)[index])
                if _decide(item, index, BreakStatus.AIRED, aired_at=on_air, stopped_at=iso(stop)):
                    aired += 1
                    logger.info("ad break aired before a stop", extra={**event, "stopped_at": iso(stop)})
            elif _decide(item, index, BreakStatus.UNAIRED):
                unaired += 1
                logger.info("ad break never aired", extra=event)
    metrics.add_metric(name="BreaksUnaired", unit=MetricUnit.Count, value=unaired)
    metrics.add_metric(name="BreaksAiredBeforeStop", unit=MetricUnit.Count, value=aired)
    return {"unaired": unaired, "aired_before_stop": aired}


def _channel(item: Mapping[str, Any]) -> str:
    return str(item["pk"]).removeprefix(CHANNEL_PREFIX)


def _stops_around(due: list[tuple[dict[str, Any], list[PlannedBreak], list[int]]]) -> dict[str, list[int]]:
    """The channels' stops over the one window that holds every pending break of ``due`` and the minute after it
    (one paginated lookup per sweep: ``LookupEvents`` allows about 2 requests a second per account and Region)."""
    times = [break_air_times(_start_ms(item), breaks)[index] for item, breaks, pending in due for index in pending]
    if not times:
        return {}
    return channel_stops(min(times), max(times) + STOP_LOSS_MS)


def channel_stops(start_ms: int, end_ms: int) -> dict[str, list[int]]:
    """When each channel was stopped from ``start_ms`` to ``end_ms`` (epoch ms): the account's successful
    ``StopChannel`` calls to MediaTailor in CloudTrail's event history, whoever made them (the auto-stop schedule,
    the agent, the console). Empty, with a warning, when the sweeper may not read the history (an SCP, say): the
    sweep then marks those breaks ``UNAIRED``, as without this lookup."""
    # OPTIMIZATION(aws): W-2 1.31 the as-run log can lose a break near StopChannel: delete this lookup when fixed
    request = {
        "LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": "StopChannel"}],
        "StartTime": datetime.fromtimestamp(start_ms / 1000, UTC),
        "EndTime": datetime.fromtimestamp(end_ms / 1000, UTC),
    }
    stops: dict[str, list[int]] = {}
    try:
        for page in aws.client("cloudtrail").get_paginator("lookup_events").paginate(**request):
            for found in page.get("Events", []):
                record = json.loads(found.get("CloudTrailEvent") or "{}")
                channel = (record.get("requestParameters") or {}).get("ChannelName")
                if record.get("eventSource") == STOP_SOURCE and not record.get("errorCode") and channel:
                    stops.setdefault(str(channel), []).append(round(found["EventTime"].timestamp() * 1000))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in DENIED:
            raise
        logger.warning("cannot read the channels' stops from CloudTrail", extra={"error": str(error)})
        return {}
    return stops


def _recent_programs(since_ms: int) -> Iterator[dict[str, Any]]:
    """``PROGRAM`` items of every channel of the deployment that start from ``since_ms`` on."""
    table = aws.table(settings().library_table)
    channels = _query(table, IndexName=GSI1, KeyConditionExpression=Key("gsi1pk").eq("CHANNELS"))
    for channel in channels:
        name = str(channel["pk"]).removeprefix(CHANNEL_PREFIX)
        yield from _query(
            table,
            KeyConditionExpression=Key("pk").eq(f"{CHANNEL_PREFIX}{name}") & Key("sk").gte(program_sk(since_ms)),
        )


def _query(table: Any, **request: Any) -> Iterator[dict[str, Any]]:
    while True:
        page = table.query(**request)
        yield from (dict(item) for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
