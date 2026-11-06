"""Delivery: our tracker's impressions, from MediaTailor's ADS log and from the CDN's logs.

Two sources, one count:

- **The ADS log.** A subscription filter on ``/aws/vendedlogs/mediatailor/<prefix>`` sends the impression beacons
  (``BEACON_FIRED``) here within seconds (``core.pds.delivery.beacon``).
- **The CDN's logs.** Every 5 minutes EventBridge Scheduler runs this function with its scheduled time. One Athena
  query reads our tracker's requests in the CloudFront log files that arrived in that run's window
  (``core.pds.delivery.cdn_log_window``, ``cdn_log_query``): each file once, 10 minutes after it arrived, and only
  the requests whose break's select record placed that ad. This is the workaround for the ADS log leaving out a
  whole break that MediaTailor filled (W-2 1.32).

For each firing of our tracker (our CDN host and path only), from either source, the function:

1. credits the impression to the ad's reservation, if it has one: RESERVED (or RELEASED, late) to DELIVERED, with
   the ledger's money event (``reservations.deliver``). It is idempotent, so it runs for every firing;
2. counts the fill once per ``(sid, evid)`` and the impression once per ``(sid, evid, crid)``, with conditional puts
   (``FILL#``, then ``IMP#``, each with the source that put it), so a firing that both sources read, or a repeated
   beacon, counts once;
3. logs a ``pds.delivery`` record with its source, which a subscription carries into the lake.

So the impressions a CDN-log run counts are the ones the ADS log left out: its metric ``CdnLogOnlyImpressions`` is the
reconciliation of the two sources, beside ``CdnLogBeacons``, the firings it read.

A batch or a run that fails is retried from the start: the reservation is credited once, and the impression put comes
last, so a retried firing is still the impression. Its fill flag can be lost when only the impression put failed, so
the lake counts fills and impressions as distinct keys, the flags marking the first firing seen.

A failed batch or run is retried by Lambda (asynchronous invocation) and then goes to the dead-letter queue; a run
redriven from there reads its own window again.
"""

import json
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.data_classes import CloudWatchLogsEvent
from botocore.exceptions import ClientError

from fast_channels.core.pbs_config import tracker_host
from fast_channels.core.pds.delivery import (
    Beacon,
    Credit,
    Ignored,
    Source,
    beacon,
    cdn_beacon,
    cdn_log_query,
    cdn_log_window,
    fill_key,
    impression_key,
)
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
QUERY_POLL_S: Final = 1.0


@cache
def host() -> str:
    """Our tracker's host: the CDN's."""
    settings().require("content_base_url")
    return tracker_host(settings().content_base_url)


def _first(table: str, key: dict[str, str], at: datetime, source: Source = Source.ADS_LOG) -> bool:
    """Put ``key`` unless it exists: whether this is its first time."""
    try:
        aws.client("dynamodb").put_item(
            TableName=table,
            Item={
                **{name: {"S": value} for name, value in key.items()},
                "at": {"S": at.isoformat(timespec="milliseconds")},
                "src": {"S": source.value},
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
    credit, item = deliver(
        aws.client("dynamodb"), table=table, ledger_table=ledger_table, decision=decision, crid=fired.crid, at=fired.at
    )
    fill = _first(table, fill_key(decision), fired.at, fired.source)
    impression = _first(table, impression_key(decision, fired.crid), fired.at, fired.source)
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
        source=fired.source,
    )


def _event(message: str) -> dict[str, Any] | None:
    try:
        event = json.loads(message)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _tally(found: Iterable[tuple[Beacon | Ignored, str]]) -> dict[str, int]:
    """Count each firing in ``found`` (with where it was read, for the warnings) and log its record."""
    config = settings()
    config.require("pds_table", "ledger_table")
    totals = {"beacons": 0, "impressions": 0, "fills": 0, "delivered": 0, "late": 0, "ignored": 0}
    for firing, where in found:
        if isinstance(firing, Ignored):
            totals["ignored"] += 1
            if firing in WARN:
                logger.warning("tracker firing ignored", extra={"reason": firing.value, "log_event": where})
            continue
        record = count(firing, table=config.pds_table, ledger_table=config.ledger_table)
        logger.set_correlation_id(record.decision_id)
        logger.info("delivery", extra=record.model_dump(mode="json"))
        totals["beacons"] += 1
        totals["impressions"] += record.impression
        totals["fills"] += record.fill
        totals["delivered"] += record.credit is Credit.DELIVERED
        totals["late"] += record.credit is Credit.LATE
    return totals


def from_ads_log(event: dict[str, Any]) -> dict[str, int]:
    """Count the impression beacons of one CloudWatch Logs subscription batch."""
    events = CloudWatchLogsEvent(event).parse_logs_data().log_events
    return _tally((beacon(_event(e.message) or {}, host=host()), e.get_id) for e in events)


def query_rows(query: str, *, workgroup: str) -> Iterator[dict[str, str | None]]:
    """The rows of one Athena query, by column name (a ``NULL`` is ``None``).

    Raises:
        RuntimeError: the query failed or was cancelled (the run is then retried).
    """
    athena = aws.client("athena")
    execution = athena.start_query_execution(QueryString=query, WorkGroup=workgroup)["QueryExecutionId"]
    while True:
        status = athena.get_query_execution(QueryExecutionId=execution)["QueryExecution"]
        match status["Status"]["State"]:
            case "SUCCEEDED":
                break
            case "FAILED" | "CANCELLED" as state:
                raise RuntimeError(f"query {execution} {state}: {status['Status'].get('StateChangeReason')}")
        time.sleep(QUERY_POLL_S)
    scanned = status.get("Statistics", {}).get("DataScannedInBytes")
    logger.info("cdn log query", extra={"query_id": execution, "scanned_bytes": scanned})
    header: list[str | None] | None = None
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=execution):
        for row in page["ResultSet"]["Rows"]:
            values = [datum.get("VarCharValue") for datum in row["Data"]]
            if header is None:
                header = values
                continue
            yield {str(name): value for name, value in zip(header, values, strict=True)}


# OPTIMIZATION(aws): W-2 1.32 the ADS log can leave out a filled break: delete this run and its schedule when fixed
def from_cdn_log(scheduled: datetime) -> dict[str, int]:
    """Count our tracker's requests in the CDN's log files that the run scheduled at ``scheduled`` reads."""
    config = settings()
    config.require("lake_workgroup", "cdn_log_table", "select_table")
    start, end = cdn_log_window(scheduled)
    logger.info("cdn log window", extra={"start": start.isoformat(), "end": end.isoformat()})
    query = cdn_log_query(start, end, cdn_table=config.cdn_log_table, select_table=config.select_table)
    rows = query_rows(query, workgroup=config.lake_workgroup)
    return _tally((cdn_beacon(row, host=host()), f"cdn:{row.get('timestamp_ms')}") for row in rows)


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    match event:
        case {"source": Source.CDN_LOG, "scheduled": str(scheduled)}:  # the schedule's run
            totals = from_cdn_log(datetime.fromisoformat(scheduled))
            metrics.add_metric(name="CdnLogBeacons", unit=MetricUnit.Count, value=totals["beacons"])
            metrics.add_metric(name="CdnLogOnlyImpressions", unit=MetricUnit.Count, value=totals["impressions"])
        case _:
            totals = from_ads_log(event)
    for name, metric in (
        ("impressions", "Impressions"),
        ("fills", "Fills"),
        ("delivered", "LineAdsDelivered"),
        ("late", "LateDeliveries"),
    ):
        metrics.add_metric(name=metric, unit=MetricUnit.Count, value=totals[name])
    logger.info("beacons counted", extra=totals)
    return totals
