"""Publish the PDS snapshot (``core.pds.snapshot``) to the private config bucket, and the own book's budgets.

The snapshot is what the deployment configures (``PDS_SNAPSHOT_BASE``: the demand allow-list, shed bidders, the
floor, the own book's lines) plus:

- CloudFront's origin-facing networks from https://ip-ranges.amazonaws.com/ip-ranges.json, which the viewer-IP rule
  needs;
- the own book's creatives: the house ads of the default ladder's kit (``ads/<ladder>/vast.xml``) and the lines'
  demo creatives (``ads/<ladder>/demo/creatives.json``), from the media bucket. The default ladder is read from
  ``/<prefix>/pds/ladder-version`` (the Media stack's) at every run, so a new ladder is bid within the hour.

It runs when the stack deploys and every hour (AWS changes its ranges without notice; the origin-facing ones rarely).
An unchanged snapshot is not written again. When ip-ranges.json cannot be read, or holds no origin-facing network, the
networks of the current snapshot are kept; when the kit cannot be read, its creatives are.

Each run then gives the lines' budget shards their share of the budgets (``reservations.set_budgets``).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.demo_demand import DemoIndex, demo_index_key
from fast_channels.core.pds.book import Book, Creative, book, house_creatives
from fast_channels.core.pds.names import Parameter, parameter_name
from fast_channels.core.pds.snapshot import (
    SNAPSHOT_KEY,
    Snapshot,
    SnapshotBase,
    build,
    digest,
    from_ip_ranges,
    history_key,
)
from fast_channels.core.pds.xff import origin_facing_networks
from fast_channels.core.specs import vast_key
from fast_channels.functions import aws
from fast_channels.functions.pds.reservations import set_budgets
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

IP_RANGES_URL: Final = "https://ip-ranges.amazonaws.com/ip-ranges.json"
TIMEOUT_SECONDS: Final = 10.0

logger = Logger()


def ip_ranges() -> dict[str, Any] | None:
    """The current ip-ranges.json, or None when it cannot be read or has no CloudFront origin-facing network."""
    try:
        response = httpx.get(IP_RANGES_URL, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        response.raise_for_status()
        document: dict[str, Any] = response.json()
    except (httpx.HTTPError, ValueError) as error:
        logger.warning("ip-ranges.json unavailable", extra={"error": type(error).__name__, "detail": str(error)})
        return None
    if not origin_facing_networks(document):
        logger.warning("ip-ranges.json lists no CLOUDFRONT_ORIGIN_FACING network")
        return None
    return document


def current(bucket: str) -> Snapshot | None:
    """The published snapshot, or None when there is none yet (or it is not a valid one)."""
    try:
        body = aws.client("s3").get_object(Bucket=bucket, Key=SNAPSHOT_KEY)["Body"].read()
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            return None
        raise
    try:
        return Snapshot.model_validate_json(body)
    except ValidationError:
        logger.warning("the published snapshot is not valid; it is replaced")
        return None


def _read(bucket: str, key: str) -> bytes:
    body: bytes = aws.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    return body


def kit(media_bucket: str, ladder_version: str, *, demo: bool) -> tuple[tuple[Creative, ...], DemoIndex | None]:
    """The house ads of a ladder's kit and, with ``demo``, its demo creatives (None when the kit has none).

    The publisher may read only the kits' two documents, not list the bucket, so S3 answers a missing demo index with
    AccessDenied rather than NoSuchKey: either means the kit has none, and the house ads are still bid.

    Raises:
        ClientError: the kit's VAST pod cannot be read.
        ValueError: it is not a pod of inline streaming ads.
    """
    house = house_creatives(_read(media_bucket, vast_key(ladder_version)).decode())
    if not demo:
        return house, None
    try:
        return house, DemoIndex.model_validate_json(_read(media_bucket, demo_index_key(ladder_version)))
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchKey", "404", "AccessDenied", "403"}:
            raise
        logger.warning(
            "the kit has no demo creatives (missing or unreadable): the lines bid nothing",
            extra={"ladder": ladder_version, "error": error.response["Error"]["Code"]},
        )
        return house, None


def own_book(base: SnapshotBase, published: Snapshot | None, *, prefix: str, media_bucket: str) -> Book:
    """The own book on the default ladder: the lines of ``base`` with the kit's creatives, or the published
    snapshot's creatives when the kit cannot be read."""
    try:
        parameter = parameter_name(prefix, Parameter.LADDER_VERSION)
        ladder = aws.client("ssm").get_parameter(Name=parameter)["Parameter"]["Value"]
        house, index = kit(media_bucket, ladder, demo=bool(base.lines))
        return book(base.lines, ladder_version=ladder, house=house, index=index)
    except (ClientError, ValueError) as error:
        logger.warning("the kit is unavailable", extra={"error": type(error).__name__, "detail": str(error)[:300]})
    if published is None:
        return Book(lines=base.lines)
    kept = published.book
    return Book(ladder_version=kept.ladder_version, lines=base.lines, creatives=kept.creatives, house=kept.house)


def publish(
    bucket: str, base: SnapshotBase, *, now: datetime, own: Callable[[Snapshot | None], Book] | None = None
) -> tuple[Snapshot, bool]:
    """Build the snapshot and write it if it changed; returns it and whether it was written.

    Args:
        bucket: the config bucket.
        base: what the deployment configures.
        now: the time of the snapshot.
        own: the own book, given the published snapshot (default: none).
    """
    published = current(bucket)
    if (document := ip_ranges()) is not None:
        networks, sync = from_ip_ranges(document)
    elif published is not None:
        networks, sync = published.origin_facing, published.ip_ranges
    else:
        networks, sync = (), None
    snapshot = build(
        base, origin_facing=networks, ip_ranges=sync, own=own(published) if own is not None else None, now=now
    )
    if published is not None and digest(published) == digest(snapshot):
        return published, False
    body = snapshot.model_dump_json(indent=1).encode()
    s3 = aws.client("s3")
    s3.put_object(Bucket=bucket, Key=history_key(snapshot), Body=body, ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=SNAPSHOT_KEY, Body=body, ContentType="application/json")
    return snapshot, True


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    config = settings()
    config.require("pds_config_bucket", "pds_snapshot_base", "prefix", "media_bucket", "pds_table", "ledger_table")
    base = SnapshotBase.model_validate_json(config.pds_snapshot_base)
    now = datetime.now(UTC)
    snapshot, written = publish(
        config.pds_config_bucket,
        base,
        now=now,
        own=lambda published: own_book(base, published, prefix=config.prefix, media_bucket=config.media_bucket),
    )
    # After the snapshot: a budget fault fails the run (and the deploy) without holding back the rest of it.
    budgets = set_budgets(
        aws.client("dynamodb"), table=config.pds_table, ledger_table=config.ledger_table, lines=base.lines, now=now
    )
    result = {
        "version": snapshot.version,
        "written": written,
        "origin_facing": len(snapshot.origin_facing),
        "bidders": sorted(snapshot.demand),
        "ladder": snapshot.ladder_version,
        "house": [c.crid for c in snapshot.house],
        "lines": {
            line.id: [c for c in line.creatives if c in {k.crid for k in snapshot.creatives}] for line in base.lines
        },
        "budgets_set": budgets,
    }
    logger.info("snapshot published" if written else "snapshot unchanged", extra=result)
    return result
