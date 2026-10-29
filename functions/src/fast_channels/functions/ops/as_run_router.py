"""Route Channel Assembly's as-run log to EventBridge: one subscription filter for every stage of an account.

The as-run log group ``MediaTailor/Channel/AsRunLog`` is shared by every channel of the account and region, and
CloudWatch Logs allows five subscription filters per log group, so a filter per stage caps an account at five
stages. With ``observability.as_run`` ``shared`` this function (in the account-level ``FastChannels-AsRun`` stack)
holds the only filter and puts every as-run event on the account's default event bus:

    {"source": "fast-channels.as-run", "detail-type": "<eventType>",
     "detail": {<the as-run event>, "as_run_id": "<log stream>/<log event id>"}}

Each stage's rule matches ``detail.channelName`` by its prefix and invokes its reconciler. ``as_run_id`` names the
log event and is the correlation id from end to end: the router logs the ids it puts, and the reconciler logs the id
of the event it handles, so ``scripts/trace.py <as_run_id>`` finds the reconciler's lines for an as-run record.

Entries are put ten at a time; entries EventBridge reports as failed are put again, and a batch that still fails
raises, so CloudWatch Logs' asynchronous invocation retries it and finally sends it to the dead-letter queue. Delivery
is therefore at least once: the reconciler's updates are conditional, so a repeated event changes nothing.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudWatchLogsEvent, event_source

from fast_channels.functions import aws

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

SOURCE: Final = "fast-channels.as-run"
"""``source`` of the routed events."""
BATCH: Final = 10
"""Entries per PutEvents call (the API's maximum)."""
ATTEMPTS: Final = 3

logger = Logger()
metrics = Metrics()


class RouteError(RuntimeError):
    """EventBridge refused entries after every attempt."""


def entries(log_stream: str, events: list[tuple[str, str]]) -> list[dict[str, str]]:
    """PutEvents entries for ``(log event id, message)`` pairs; messages that are no as-run event are skipped."""
    found = []
    for event_id, message in events:
        try:
            record = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("not JSON; skipped", extra={"as_run_id": f"{log_stream}/{event_id}"})
            continue
        if not isinstance(record, dict) or not isinstance(record.get("eventType"), str):
            logger.warning("not an as-run event; skipped", extra={"as_run_id": f"{log_stream}/{event_id}"})
            continue
        found.append(
            {
                "Source": SOURCE,
                "DetailType": record["eventType"],
                "Detail": json.dumps({**record, "as_run_id": f"{log_stream}/{event_id}"}),
            }
        )
    return found


def put(batch: list[dict[str, str]]) -> None:
    """Put ``batch`` (at most :data:`BATCH` entries), putting failed entries again up to :data:`ATTEMPTS` times.

    Raises:
        RouteError: entries still failed.
    """
    pending = batch
    for _ in range(ATTEMPTS):
        response: dict[str, Any] = aws.client("events").put_events(Entries=pending)
        if not response.get("FailedEntryCount"):
            logger.info(
                "as-run events put", extra={"as_run_ids": [json.loads(e["Detail"])["as_run_id"] for e in pending]}
            )
            return
        pending = [entry for entry, result in zip(pending, response["Entries"], strict=True) if "ErrorCode" in result]
        logger.warning("entries refused; putting them again", extra={"refused": len(pending)})
    raise RouteError(f"EventBridge refused {len(pending)} as-run entries after {ATTEMPTS} attempts")


@metrics.log_metrics
@logger.inject_lambda_context
@event_source(data_class=CloudWatchLogsEvent)
def handler(event: CloudWatchLogsEvent, context: LambdaContext) -> dict[str, int]:
    """Put every as-run event of the subscription batch on the default bus."""
    data = event.parse_logs_data()
    routed = entries(data.log_stream, [(e.get_id, e.message) for e in data.log_events])
    if routed:
        logger.set_correlation_id(json.loads(routed[0]["Detail"])["as_run_id"])
    for start in range(0, len(routed), BATCH):
        put(routed[start : start + BATCH])
    logger.info("as-run events routed", extra={"log_stream": data.log_stream, "events": len(routed)})
    metrics.add_metric(name="AsRunEventsRouted", unit=MetricUnit.Count, value=len(routed))
    return {"routed": len(routed)}
