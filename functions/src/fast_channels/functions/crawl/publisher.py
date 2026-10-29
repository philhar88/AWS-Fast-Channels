"""Catalog context publisher: keeps ``catalog/ctx/v1/<content_id>.json`` in the media bucket in step with the
reference catalog (docs/content-enrichment.md).

Each object mirrors one ``ReferenceCatalog`` title. It is written while the title is in the catalog and deleted when
the title is not, so the lookup of a removed title misses. Three triggers:

- ``Schedule Published``: every title of the published plan (its ``PROGRAM`` items in the media library);
- ``Catalog Item Changed``: the title a CMS sync job has just written;
- the daily sweep (``{"sweep": true}`` from EventBridge Scheduler): every catalog title, and the deletion of each
  object whose title has left the catalog.

An object is rewritten only when its content changes: the ``digest`` in its metadata is compared first (one HEAD).
So ``updated`` says when the metadata last changed, and an unchanged catalog causes no writes. The objects hold
catalog metadata only, never session, player or viewer data.
"""

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CACHE_CONTROL, CONTEXT_PREFIX, build_context, context_key, digest
from fast_channels.core.crawl.taxonomy import default_genre_map
from fast_channels.core.events import DETAIL_TYPES, CatalogItemChanged, SchedulePublished
from fast_channels.functions import aws
from fast_channels.functions.media_library import CHANNEL_PREFIX, program_sk
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store as store

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from mypy_boto3_dynamodb.service_resource import Table

DIGEST_METADATA: Final = "digest"
"""User metadata (``x-amz-meta-digest``) holding :func:`~fast_channels.core.crawl.catalog_context.digest`."""
CONTENT_TYPE: Final = "application/json"
_MISSING: Final = frozenset({"404", "403", "NoSuchKey", "NotFound"})
"""HEAD of an absent key: 404 with ``s3:ListBucket`` on the prefix, 403 where the bucket withholds it."""

logger = Logger()
metrics = Metrics()


class Trigger(StrEnum):
    """EventBridge detail types the publisher handles."""

    SCHEDULE_PUBLISHED = DETAIL_TYPES[SchedulePublished]
    CATALOG_ITEM_CHANGED = DETAIL_TYPES[CatalogItemChanged]


class Outcome(StrEnum):
    """What happened to one title's object."""

    WRITTEN = "written"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    INVALID = "invalid"
    """The catalog item fails the contract (logged as an error and left as it was): retrying cannot help."""


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: Mapping[str, Any], context: LambdaContext) -> dict[str, int]:
    """Publish the titles an event names, or sweep the whole catalog; returns the count of each outcome."""
    settings().require("catalog_table", "media_bucket")
    updated = datetime.now(UTC)
    match event:
        case {"detail-type": Trigger.SCHEDULE_PUBLISHED, "detail": Mapping() as detail}:
            published = SchedulePublished.model_validate(detail)
            logger.set_correlation_id(published.correlation_id)
            outcomes = Counter(publish(content_id, updated=updated) for content_id in plan_titles(published))
        case {"detail-type": Trigger.CATALOG_ITEM_CHANGED, "detail": Mapping() as detail}:
            changed = CatalogItemChanged.model_validate(detail)
            logger.set_correlation_id(changed.correlation_id or event.get("id"))
            outcomes = Counter([publish(changed.content_id, updated=updated)])
        case {"sweep": True}:
            logger.set_correlation_id(f"crawl-sweep-{updated:%Y%m%dT%H%M%SZ}")
            outcomes = sweep(updated=updated)
        case _:
            raise ValueError(f"not a publisher event: keys {sorted(event)}")
    for outcome in Outcome:
        metrics.add_metric(name=f"Contexts{outcome.title()}", unit=MetricUnit.Count, value=outcomes[outcome])
    summary = {outcome.value: outcomes[outcome] for outcome in Outcome}
    logger.info("catalog contexts published", extra=summary)
    return summary


def plan_titles(published: SchedulePublished) -> list[str]:
    """Content ids of the published plan's programs, in air order, without repeats.

    Only the two attributes it needs are read from the plan window's ``PROGRAM`` items.
    """
    settings().require("library_table")
    items = store.query_all(
        KeyConditionExpression=Key("pk").eq(f"{CHANNEL_PREFIX}{published.channel}")
        & Key("sk").between(program_sk(published.start_ms), program_sk(published.end_ms)),
        ProjectionExpression="content_id, plan_id",
    )
    ids = (
        str(item["content_id"]) for item in items if item.get("plan_id") == published.plan_id and item.get("content_id")
    )
    return list(dict.fromkeys(ids))


def publish(content_id: str, *, updated: datetime) -> Outcome:
    """Bring the object of ``content_id`` in line with its catalog item."""
    item = _catalog().get_item(Key=_catalog_key(content_id)).get("Item")
    return reconcile(content_id, store.plain(item) if item else None, updated=updated)


def sweep(*, updated: datetime) -> Counter[Outcome]:
    """Publish every catalog title, then delete the objects of titles no longer in the catalog."""
    outcomes: Counter[Outcome] = Counter()
    titles: set[str] = set()
    for item in _catalog_items():
        content_id = str(item.get("content_id", ""))
        titles.add(content_id)
        outcomes[reconcile(content_id, item, updated=updated)] += 1
    for content_id in _published_ids():
        if content_id not in titles:
            outcomes[reconcile(content_id, None, updated=updated)] += 1
    return outcomes


def reconcile(content_id: str, item: Mapping[str, Any] | None, *, updated: datetime) -> Outcome:
    """Write the object of ``item`` if its content changed, or delete it when ``item`` is None."""
    config = settings()
    s3, key = aws.client("s3"), context_key(content_id)
    if item is None:
        s3.delete_object(Bucket=config.media_bucket, Key=key)
        logger.info("catalog context deleted", extra={"content_id": content_id, "key": key})
        return Outcome.DELETED
    try:
        built = build_context(
            ContentItem.model_validate(item),
            genre_map=default_genre_map(),
            publisher_domain=config.publisher_domain or None,
            updated=updated,
        )
    except ValueError as error:  # pydantic's ValidationError included
        logger.error("catalog item not published", extra={"content_id": content_id, "error": str(error)[:1000]})
        return Outcome.INVALID
    new = digest(built)
    if _current_digest(key) == new:
        return Outcome.UNCHANGED
    s3.put_object(
        Bucket=config.media_bucket,
        Key=key,
        Body=built.model_dump_json().encode(),
        ContentType=CONTENT_TYPE,
        CacheControl=CACHE_CONTROL,
        Metadata={DIGEST_METADATA: new},
    )
    logger.info("catalog context written", extra={"content_id": content_id, "key": key, "digest": new})
    return Outcome.WRITTEN


def _current_digest(key: str) -> str | None:
    try:
        head = aws.client("s3").head_object(Bucket=settings().media_bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _MISSING:
            return None
        raise
    found: str | None = head.get("Metadata", {}).get(DIGEST_METADATA)
    return found


def _catalog() -> Table:
    return aws.table(settings().catalog_table)


def _catalog_key(content_id: str) -> dict[str, str]:
    """``ReferenceCatalog`` key of a title (``tools.catalog.catalog_item``)."""
    return {"pk": f"CONTENT#{content_id}", "sk": "META"}


def _catalog_items() -> Iterator[dict[str, Any]]:
    """Every title of the reference catalog (all scan pages), with plain numbers."""
    request: dict[str, Any] = {"FilterExpression": "sk = :meta", "ExpressionAttributeValues": {":meta": "META"}}
    while True:
        page = _catalog().scan(**request)
        yield from (store.plain(item) for item in page.get("Items", []))
        if not (start := page.get("LastEvaluatedKey")):
            return
        request["ExclusiveStartKey"] = start


def _published_ids() -> Iterable[str]:
    """Content ids of the objects under :data:`CONTEXT_PREFIX`."""
    pages = (
        aws.client("s3")
        .get_paginator("list_objects_v2")
        .paginate(Bucket=settings().media_bucket, Prefix=CONTEXT_PREFIX)
    )
    for page in pages:
        for entry in page.get("Contents", []):
            name = entry["Key"].removeprefix(CONTEXT_PREFIX)
            if name.endswith(".json") and "/" not in name:
                yield name.removesuffix(".json")
