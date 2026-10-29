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
example after the channel stopped.
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

from fast_channels.core.as_run import matching_break, unaired_breaks
from fast_channels.core.timeline import PlannedBreak
from fast_channels.functions import aws
from fast_channels.functions.media_library import (
    CHANNEL_PREFIX,
    GSI1,
    ProgramStatus,
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
        logger.append_keys(event_id=event.get("id"))
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


def _decide(item: Mapping[str, Any], index: int, status: BreakStatus, *, aired_at: str | None = None) -> bool:
    """Set break ``index``'s status (and ``aired_at``) on the program item, unless it is decided already."""
    names = {"#breaks": "breaks", "#status": "status"}
    values: dict[str, str] = {":status": status.value}
    update = f"SET #breaks[{index}].#status = :status"
    if aired_at is not None:
        update += f", #breaks[{index}].aired_at = :at"
        values[":at"] = aired_at
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
    ``AdBreakStart`` (``core.as_run.unaired_breaks``); skipped programs' breaks never air either."""
    settings().require("library_table")
    now_ms = _now_ms()
    unaired = 0
    for item in _recent_programs(now_ms - SWEEP_WINDOW_MS):
        breaks = _breaks(item)
        decided = {i for i, b in enumerate(item.get("breaks") or []) if b.get("status")}
        if item.get("status") == ProgramStatus.SKIPPED.value:
            pending = [i for i in range(len(breaks)) if i not in decided]
        else:
            pending = unaired_breaks(now_ms, _start_ms(item), breaks, decided)
        for index in pending:
            if _decide(item, index, BreakStatus.UNAIRED):
                unaired += 1
                logger.info(
                    "ad break never aired",
                    extra={"program": item.get("name"), "break": index, "event_id": breaks[index].event_id},
                )
    metrics.add_metric(name="BreaksUnaired", unit=MetricUnit.Count, value=unaired)
    return {"unaired": unaired}


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
