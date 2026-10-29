"""The release sweeper: gives back the price of reservations that saw no impression in two hours.

Every 15 minutes it reads the table's sparse ``pending`` index (open reservations by time) for those reserved more
than :data:`~fast_channels.core.pds.delivery.RELEASE_AFTER` ago, and releases each one with its ledger money event
(``reservations.release``). MediaTailor retries a beacon for up to an hour, so an impression that does arrive later
is still delivered, late. It stops early when the invocation is close to its timeout; the next run carries on.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.pds.delivery import PENDING, RELEASE_AFTER
from fast_channels.core.pds.names import PENDING_INDEX
from fast_channels.functions import aws
from fast_channels.functions.pds.reservations import release
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from aws_lambda_powertools.utilities.typing import LambdaContext

PAGE: Final = 100
MARGIN_MS: Final = 10_000
"""Stop when less than this is left of the invocation."""

logger = Logger()
metrics = Metrics()


def due(client: Any, table: str, *, before: datetime) -> Iterator[tuple[str, str]]:
    """The open reservations made before ``before``: (decision id, crid), oldest first."""
    request: dict[str, Any] = {
        "TableName": table,
        "IndexName": PENDING_INDEX,
        "KeyConditionExpression": "pending = :pending AND pending_at < :before",
        "ExpressionAttributeValues": {
            ":pending": {"S": PENDING},
            ":before": {"S": before.isoformat(timespec="milliseconds")},
        },
        "ProjectionExpression": "pk, sk",
        "Limit": PAGE,
    }
    while True:
        page = client.query(**request)
        for item in page.get("Items", []):
            yield item["pk"]["S"].removeprefix("RES#"), item["sk"]["S"]
        if "LastEvaluatedKey" not in page:
            return
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sweep(table: str, ledger_table: str, *, now: datetime, time_left: Callable[[], int]) -> dict[str, int]:
    """Release what is due; returns how many were released, and found no longer open."""
    client = aws.client("dynamodb")
    totals = {"released": 0, "skipped": 0}
    for decision, crid in due(client, table, before=now - RELEASE_AFTER):
        if time_left() < MARGIN_MS:
            logger.info("stopping before the timeout; the next run carries on")
            break
        item = release(client, table=table, ledger_table=ledger_table, decision=decision, crid=crid, now=now)
        totals["released" if item is not None else "skipped"] += 1
    return totals


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    config = settings()
    config.require("pds_table", "ledger_table")
    totals = sweep(
        config.pds_table, config.ledger_table, now=datetime.now(UTC), time_left=context.get_remaining_time_in_millis
    )
    metrics.add_metric(name="ReservationsReleased", unit=MetricUnit.Count, value=totals["released"])
    logger.info("reservations swept", extra=totals)
    return totals
