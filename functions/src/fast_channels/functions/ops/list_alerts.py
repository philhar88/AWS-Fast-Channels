"""Publish ``Channel Alert`` events from Channel Assembly's alerts (ops module).

Channel Assembly and MediaTailor publish no events, so an EventBridge Scheduler schedule invokes this function every
minute with its scheduled time. While the channel census counts no running channel (``/<prefix>/ops/running-channels``
is ``0``) it does nothing. Otherwise it reads ``ListAlerts`` for each running channel of the deployment (by its
``fast:app`` tag) and puts one ``Channel Alert`` (``core.events.ChannelAlert``) on the bus for every alert whose last
change falls in the minute before the scheduled time, so each change is published once, without state, and a retried
invocation (same scheduled time) publishes the same events. The expected warnings that the design raises by
construction (``tools._alerts.NOISE_ALERTS``) are left out.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.events import ChannelAlert, put_events_entry
from fast_channels.core.tags import TAG_APP
from fast_channels.functions import aws
from fast_channels.functions.media_library import iso
from fast_channels.functions.settings import settings
from fast_channels.functions.tools._alerts import is_noise

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

WINDOW: Final = timedelta(minutes=1)
"""The schedule's interval: each poll publishes the alerts changed in the window before its scheduled time."""
BATCH: Final = 10

logger = Logger()
metrics = Metrics()


def running_channels(prefix: str) -> list[dict[str, Any]]:
    """The deployment's RUNNING channels (``fast:app`` tag = ``prefix``)."""
    pages = aws.client("mediatailor").get_paginator("list_channels").paginate()
    return [
        channel
        for page in pages
        for channel in page.get("Items", [])
        if channel.get("ChannelState") == "RUNNING" and (channel.get("Tags") or {}).get(TAG_APP) == prefix
    ]


def census_running(parameter: str) -> int:
    """The running channels at the last census that saw a change."""
    return int(aws.client("ssm").get_parameter(Name=parameter)["Parameter"]["Value"])


def changed_alerts(channel: dict[str, Any], start: datetime, end: datetime, correlation_id: str) -> list[ChannelAlert]:
    """Alerts of ``channel`` whose last change is in ``[start, end)``, expected warnings left out."""
    pages = aws.client("mediatailor").get_paginator("list_alerts").paginate(ResourceArn=channel["Arn"])
    return [
        ChannelAlert(
            channel=channel["ChannelName"],
            code=item["AlertCode"],
            category=item.get("Category", ""),
            message=str(item.get("AlertMessage", ""))[:400],
            resource_arn=item.get("ResourceArn", channel["Arn"]),
            last_modified=iso(item["LastModifiedTime"]),
            correlation_id=correlation_id,
        )
        for page in pages
        for item in page.get("Items", [])
        if not is_noise(item) and start <= item["LastModifiedTime"] < end
    ]


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int | str]:
    """Publish the alerts of the deployment's running channels that changed in the minute before ``scheduled_time``."""
    config = settings()
    config.require("prefix", "event_bus", "census_parameter")
    end = datetime.fromisoformat(event["scheduled_time"]).astimezone(UTC)
    correlation_id = end.isoformat().replace("+00:00", "Z")
    logger.set_correlation_id(f"alerts-{correlation_id}")
    if census_running(config.census_parameter) == 0:
        return {"skipped": "no channel runs"}
    alerts = [
        alert
        for channel in running_channels(config.prefix)
        for alert in changed_alerts(channel, end - WINDOW, end, correlation_id)
    ]
    entries = [put_events_entry(alert, prefix=config.prefix, event_bus=config.event_bus) for alert in alerts]
    for start in range(0, len(entries), BATCH):
        response = aws.client("events").put_events(Entries=entries[start : start + BATCH])
        if response.get("FailedEntryCount"):
            raise RuntimeError(f"EventBridge refused {response['FailedEntryCount']} Channel Alert events")
    for alert in alerts:
        logger.info("channel alert", extra={"alert": alert.model_dump()})
    metrics.add_metric(name="ChannelAlerts", unit=MetricUnit.Count, value=len(alerts))
    return {"published": len(alerts)}
