"""Publish the PDS snapshot (``core.pds.snapshot``) to the private config bucket.

The snapshot is what the deployment configures (``PDS_SNAPSHOT_BASE``: the demand allow-list, shed bidders, the
floor) plus CloudFront's origin-facing networks from https://ip-ranges.amazonaws.com/ip-ranges.json, which the
viewer-IP rule needs. It runs when the stack deploys and every hour (AWS changes its ranges without notice; the
origin-facing ones rarely). An unchanged snapshot is not written again. When ip-ranges.json cannot be read, or holds
no origin-facing network, the networks of the current snapshot are kept.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import httpx
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot, SnapshotBase, build, digest, history_key
from fast_channels.core.pds.xff import origin_facing_networks
from fast_channels.functions import aws
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


def publish(bucket: str, base: SnapshotBase, *, now: datetime) -> tuple[Snapshot, bool]:
    """Build the snapshot and write it if it changed; returns it and whether it was written."""
    published = current(bucket)
    document = ip_ranges()
    if document is None and published is not None:
        document = {
            "syncToken": published.ip_ranges,
            "prefixes": [{"ip_prefix": n, "service": "CLOUDFRONT_ORIGIN_FACING"} for n in published.origin_facing],
        }
    snapshot = build(base, document, now=now)
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
    config.require("pds_config_bucket", "pds_snapshot_base")
    snapshot, written = publish(
        config.pds_config_bucket, SnapshotBase.model_validate_json(config.pds_snapshot_base), now=datetime.now(UTC)
    )
    result = {
        "version": snapshot.version,
        "written": written,
        "origin_facing": len(snapshot.origin_facing),
        "bidders": sorted(snapshot.demand),
    }
    logger.info("snapshot published" if written else "snapshot unchanged", extra=result)
    return result
