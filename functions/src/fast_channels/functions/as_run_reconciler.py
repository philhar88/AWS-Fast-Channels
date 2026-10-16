"""Mark scheduled programs ``AIRED`` from Channel Assembly's as-run log.

``AsRunLogging`` subscribes this function to ``MediaTailor/Channel/AsRunLog`` with a filter for ``VodProgramStart``
events of the deployment's channels. Channel Assembly writes one event per program start and output, e.g.
``{"channelName": "fast-demo", "eventType": "VodProgramStart", "programName": "20261016T120000-bbb-1a2b3c",
"sourceGroup": "hls", "timeStamp": "2026-10-16T12:00:03.170Z", ...}`` (channel-assembly.md §7.2), so the first
event of a start updates the program's ``PROGRAM`` item and the duplicates of the other outputs are no-ops.
Programs the media library does not know (published outside the schedule tools) are ignored.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudWatchLogsEvent, event_source
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

PROGRAM_START: Final = "VodProgramStart"
AIRED: Final = "AIRED"
GSI1: Final = "GSI1"

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
@event_source(data_class=CloudWatchLogsEvent)
def handler(event: CloudWatchLogsEvent, context: LambdaContext) -> None:
    """Mark the program of every ``VodProgramStart`` event in the batch as aired."""
    settings().require("library_table")
    data = event.parse_logs_data()
    logger.append_keys(log_stream=data.log_stream)
    aired = 0
    for log_event in data.log_events:
        try:
            start = AsRunEvent.model_validate_json(log_event.message)
        except ValidationError:
            logger.warning("not an as-run event", extra={"log_message": log_event.message[:500]})
            continue
        if start.event_type == PROGRAM_START and start.program and mark_aired(start):
            aired += 1
    metrics.add_metric(name="ProgramsAired", unit=MetricUnit.Count, value=aired)


def mark_aired(start: AsRunEvent) -> bool:
    """Set the program's status to AIRED (once); ``False`` if unknown or already aired."""
    table = aws.table(settings().library_table)
    items = table.query(
        IndexName=GSI1,
        KeyConditionExpression=Key("gsi1pk").eq(f"PROGRAM#{start.program}") & Key("gsi1sk").eq(start.channel),
    ).get("Items", [])
    if not items:
        logger.info("program not in the media library", extra={"channel": start.channel, "program": start.program})
        return False
    item = items[0]  # program names are unique per channel (Channel Assembly rejects duplicates)
    logger.set_correlation_id(_text(item.get("correlation_id")))
    try:
        table.update_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            UpdateExpression="SET #status = :aired, aired_at = :at, updated_at = :now",
            ConditionExpression="attribute_exists(pk) AND #status <> :aired",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":aired": AIRED,
                ":at": start.timestamp,
                ":now": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return False
    logger.info("program aired", extra={"channel": start.channel, "program": start.program, "at": start.timestamp})
    return True


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None
