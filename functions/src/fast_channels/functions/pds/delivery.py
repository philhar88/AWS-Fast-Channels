"""Delivery: our tracker's impressions, from the ``BEACON_FIRED`` events of the stage's MediaTailor log group.

A subscription filter on ``/aws/vendedlogs/mediatailor/<prefix>`` sends the impression beacons here. For each firing
of our tracker (our CDN host and path only; ``core.pds.delivery.beacon``) the function:

1. counts the impression once per ``(sid, evid, crid)`` and the fill once per ``(sid, evid)``, with conditional puts
   (``IMP#`` and ``FILL#`` items), so a repeated beacon counts once;
2. credits the impression to the ad's reservation, if it has one: RESERVED (or RELEASED, late) to DELIVERED, with the
   ledger's money event (``reservations.deliver``); this step runs for repeated beacons too, so a retry after a
   failure finishes the job;
3. logs a ``pds.delivery`` record, which a subscription carries into the lake.

A failed batch is retried by Lambda (asynchronous invocation) and then goes to the dead-letter queue.
"""

import json
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudWatchLogsEvent
from botocore.exceptions import ClientError

from fast_channels.core.pbs_config import tracker_host
from fast_channels.core.pds.delivery import Beacon, Credit, Ignored, beacon, fill_key, impression_key
from fast_channels.core.pds.records import DeliveryRecord
from fast_channels.functions import aws
from fast_channels.functions.pds.reservations import CLEANUP, deliver
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

WARN: frozenset[Ignored] = frozenset({Ignored.MALFORMED, Ignored.SESSION_MISMATCH})
"""Firings of our tracker that should not happen: logged."""


@cache
def host() -> str:
    """Our tracker's host: the CDN's."""
    settings().require("content_base_url")
    return tracker_host(settings().content_base_url)


def _first(table: str, key: dict[str, str], at: datetime) -> bool:
    """Put ``key`` unless it exists: whether this is its first time."""
    try:
        aws.client("dynamodb").put_item(
            TableName=table,
            Item={
                **{name: {"S": value} for name, value in key.items()},
                "at": {"S": at.isoformat(timespec="milliseconds")},
                "ttl": {"N": str(int((at + CLEANUP).timestamp()))},
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def count(fired: Beacon, *, table: str, ledger_table: str) -> DeliveryRecord:
    """Count one firing of our tracker and credit it to its reservation."""
    decision = fired.decision_id
    impression = _first(table, impression_key(decision, fired.crid), fired.at)
    fill = impression and _first(table, fill_key(decision), fired.at)
    credit, item = deliver(
        aws.client("dynamodb"), table=table, ledger_table=ledger_table, decision=decision, crid=fired.crid, at=fired.at
    )
    return DeliveryRecord(
        decision_id=decision,
        sid=fired.sid,
        evid=fired.evid,
        crid=fired.crid,
        enr=fired.enr,
        pc=fired.configuration,
        fired_at=fired.at.astimezone(UTC).isoformat(timespec="milliseconds"),
        status=fired.status,
        impression=impression,
        fill=fill,
        credit=credit,
        line=item["line"]["S"] if item is not None else None,
        amount=item["amount"]["N"] if item is not None else None,
    )


def _event(message: str) -> dict[str, Any] | None:
    try:
        event = json.loads(message)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    config = settings()
    config.require("pds_table", "ledger_table")
    totals = {"beacons": 0, "impressions": 0, "fills": 0, "delivered": 0, "late": 0, "ignored": 0}
    for log_event in CloudWatchLogsEvent(event).parse_logs_data().log_events:
        found = beacon(_event(log_event.message) or {}, host=host())
        if isinstance(found, Ignored):
            totals["ignored"] += 1
            if found in WARN:
                logger.warning("tracker firing ignored", extra={"reason": found.value, "log_event": log_event.get_id})
            continue
        record = count(found, table=config.pds_table, ledger_table=config.ledger_table)
        logger.set_correlation_id(record.decision_id)
        logger.info("delivery", extra=record.model_dump(mode="json"))
        totals["beacons"] += 1
        totals["impressions"] += record.impression
        totals["fills"] += record.fill
        totals["delivered"] += record.credit is Credit.DELIVERED
        totals["late"] += record.credit is Credit.LATE
    for name, metric in (
        ("impressions", "Impressions"),
        ("fills", "Fills"),
        ("delivered", "LineAdsDelivered"),
        ("late", "LateDeliveries"),
    ):
        metrics.add_metric(name=metric, unit=MetricUnit.Count, value=totals[name])
    logger.info("beacons counted", extra=totals)
    return totals
