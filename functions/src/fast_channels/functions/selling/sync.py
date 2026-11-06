"""The seller's sync: booked buys in, own-book lines and delivery out (``core.selling.booking``).

The seller's bridge (``vendor/agentic-selling/seller/fast/bridge.py``, a container of the seller's task) invokes it
synchronously, so the seller's database never leaves its VPC and this function never needs it:

- ``{"action": "products"}`` answers the products (``core.selling.products``): the monetized channels with the
  catalog's genres, read from the catalog table at each call. The bridge stores them in the seller.
- ``{"action": "sync", "buys": [...]}`` maps the tenant's buys to lines, keeping each live line's creative. When the
  lines changed it writes them to the PDS config bucket (``lines/booked.json``) and runs the snapshot publisher, which
  publishes them with their budgets; the sidecars read the new snapshot within 30 seconds. It answers each buy's
  delivery, from its lines' budget shards (``delivered``), which the bridge writes where the seller's
  ``get_media_buy_delivery`` reads it.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict

from fast_channels.core.pds.delivery import BUDGET_SHARDS, budget_key
from fast_channels.core.pds.snapshot import PublishedLines
from fast_channels.core.selling.booking import Buy, booked_lines, delivery
from fast_channels.core.selling.products import Terms
from fast_channels.functions import aws
from fast_channels.functions.pds.reservations import item_key
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

    from fast_channels.core.pds.book import Line

BATCH: Final = 100
"""``BatchGetItem``'s most keys per call."""
REQUIRED: Final = (
    "selling_products",
    "catalog_table",
    "pds_config_bucket",
    "pds_booked_lines_key",
    "pds_table",
    "pds_publisher_function",
)

logger = Logger()


class Request(BaseModel):
    """The bridge's call."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["products", "sync"]
    buys: tuple[Buy, ...] = ()


def genres(table: str) -> list[str]:
    """Every genre of the catalog's titles (a projection scan of the catalog table)."""
    found: set[str] = set()
    paginator = aws.client("dynamodb").get_paginator("scan")
    for page in paginator.paginate(TableName=table, ProjectionExpression="genres"):
        for item in page.get("Items", []):
            found.update(value["S"] for value in item.get("genres", {}).get("L", []) if "S" in value)
    return sorted(found)


def current(bucket: str, key: str) -> PublishedLines:
    """The booked lines published before (none at first)."""
    try:
        body = aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return PublishedLines()
        raise
    return PublishedLines.model_validate_json(body)


def delivered(table: str, lines: Sequence[Line]) -> dict[str, Decimal]:
    """What each line's budget shards delivered, summed (a line with no shard yet is left out)."""
    keys = [item_key(budget_key(line.id, shard)) for line in lines for shard in range(BUDGET_SHARDS)]
    sums: dict[str, Decimal] = {}
    dynamodb = aws.client("dynamodb")
    for start in range(0, len(keys), BATCH):
        request: dict[str, Any] = {
            table: {"Keys": keys[start : start + BATCH], "ProjectionExpression": "pk, delivered"}
        }
        while request:
            response = dynamodb.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table, []):
                line = item["pk"]["S"].removeprefix("BUDGET#")
                sums[line] = sums.get(line, Decimal(0)) + Decimal(item.get("delivered", {}).get("N", "0"))
            request = response.get("UnprocessedKeys") or {}
    return sums


def publish(bucket: str, key: str, lines: Iterable[Line], publisher: str) -> None:
    """Write the booked lines and run the snapshot publisher (asynchronously: it retries and dead-letters)."""
    document = PublishedLines(lines=tuple(lines))
    aws.client("s3").put_object(
        Bucket=bucket, Key=key, Body=document.model_dump_json(indent=1).encode(), ContentType="application/json"
    )
    aws.client("lambda").invoke(FunctionName=publisher, InvocationType="Event", Payload=b"{}")


def sync(buys: Sequence[Buy], terms: Terms, *, now: datetime) -> dict[str, Any]:
    """The booked lines published when they changed, and each booked buy's delivery."""
    config = settings()
    before = current(config.pds_config_bucket, config.pds_booked_lines_key)
    booking = booked_lines(buys, products=terms.products(()), pool=terms.creatives, current=before.lines, now=now)
    changed = booking.lines != before.lines
    if changed:
        publish(config.pds_config_bucket, config.pds_booked_lines_key, booking.lines, config.pds_publisher_function)
    reports = delivery(booking, delivered(config.pds_table, booking.lines))
    logger.info(
        "booked lines published" if changed else "booked lines unchanged",
        extra={
            "buys": len(buys),
            "lines": {line.id: line.creatives[0] for line in booking.lines},
            "unserved": {key: reason.value for key, reason in booking.unserved.items()},
            "impressions": {report.media_buy_id: report.impressions for report in reports},
        },
    )
    return {
        "changed": changed,
        "lines": {line_id: list(package) for line_id, package in booking.packages.items()},
        "unserved": {key: reason.value for key, reason in booking.unserved.items()},
        "delivery": [report.model_dump(mode="json") for report in reports],
    }


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    config = settings()
    config.require(*REQUIRED)
    terms = Terms.model_validate_json(config.selling_products)
    request = Request.model_validate(event)
    match request.action:
        case "products":
            products = terms.products(genres(config.catalog_table))
            return {"products": [product.model_dump(mode="json") for product in products]}
        case "sync":
            return sync(request.buys, terms, now=datetime.now(UTC))
