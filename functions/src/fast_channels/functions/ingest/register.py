"""``Register``: create or refresh the Channel Assembly VOD source of a content or slate encode.

The package configurations are the deployment's package profiles that the ladder has (Finalize lists them),
with paths relative to the S3 source location's base URL (``core.specs.http_package_configurations``). An
existing VOD source is updated, which also makes Channel Assembly re-read manifests it has cached
(channel-assembly.md §2.5). CreateVodSource only validates access synchronously; problems found while processing
the manifests (e.g. ``UNAUTHORIZED``) surface later as alerts (§2.4, §7.4), so the step then polls ListAlerts,
bounded, until no SCHEDULING_ERROR alert remains.

Channel Assembly allows 1 TPS for these calls: the state machine runs this step in a Map with MaxConcurrency 1
and the shared client retries throttling adaptively.
"""

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from fast_channels.core.profiles import profile
from fast_channels.core.specs import http_package_configurations
from fast_channels.functions import aws
from fast_channels.functions.ingest.library import runtime_tags
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

BLOCKING_CATEGORY: Final = "SCHEDULING_ERROR"
"""Alert category that keeps a VOD source off the air; PLAYBACK_WARNING and INFO alerts are only reported."""
ALERT_SETTLE_SECONDS: Final = 10
ALERT_POLLS: Final = 6
_NOT_FOUND: Final = frozenset({"NotFoundException", "ResourceNotFoundException"})
_BUSY: Final = frozenset({"ConflictException", "TooManyRequestsException", "ThrottlingException"})

logger = Logger()


class VodSourceAlertError(RuntimeError):
    """Channel Assembly reports scheduling errors for the new VOD source."""


class VodSourceBusyError(RuntimeError):
    """Channel Assembly throttled the call or holds a lock on the VOD source; the state machine retries."""


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One Finalize registration -> ``{vod_source, vod_source_arn, source_location, packages, alerts}``."""
    config = settings()
    config.require("source_location", "prefix")
    logger.set_correlation_id(event["correlation_id"])
    logger.append_keys(asset_id=event["asset_id"], ladder_version=event["ladder_version"])
    packages = http_package_configurations(event["output_prefix"], [profile(name) for name in event["profiles"]])
    if not packages:
        raise VodSourceAlertError("none of the deployment's package profiles applies to this ladder")
    tags = runtime_tags(
        config.prefix,
        ladder_version=event["ladder_version"],
        correlation_id=event["correlation_id"],
        asset=event["asset_id"],
    )
    try:
        arn = upsert(config.source_location, event["vod_source"], packages, tags)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _BUSY:
            raise VodSourceBusyError(str(error)) from error
        raise
    alerts = wait_for_alerts(arn)
    logger.info("VOD source registered", extra={"vod_source_arn": arn, "alerts": alerts})
    return {
        "vod_source": event["vod_source"],
        "vod_source_arn": arn,
        "source_location": config.source_location,
        "packages": {p["SourceGroup"]: p["Path"] for p in packages},
        "alerts": alerts,
    }


def upsert(source_location: str, name: str, packages: list[dict[str, str]], tags: Mapping[str, str]) -> str:
    """Create the VOD source, or update its package configurations and tags; returns its ARN."""
    mediatailor = aws.client("mediatailor")
    try:
        mediatailor.describe_vod_source(SourceLocationName=source_location, VodSourceName=name)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in _NOT_FOUND:
            raise
        created = mediatailor.create_vod_source(
            SourceLocationName=source_location, VodSourceName=name, HttpPackageConfigurations=packages, Tags=dict(tags)
        )
        return str(created["Arn"])
    updated = mediatailor.update_vod_source(
        SourceLocationName=source_location, VodSourceName=name, HttpPackageConfigurations=packages
    )
    mediatailor.tag_resource(ResourceArn=updated["Arn"], Tags=dict(tags))
    return str(updated["Arn"])


def wait_for_alerts(arn: str) -> list[str]:
    """Poll ListAlerts until no SCHEDULING_ERROR alert remains; returns the remaining (non-blocking) alert codes.

    Raises:
        VodSourceAlertError: scheduling errors are still reported after the last poll.
    """
    blocking: list[dict[str, Any]] = []
    for _ in range(ALERT_POLLS):
        time.sleep(ALERT_SETTLE_SECONDS)
        alerts = _alerts(arn)
        if not (blocking := [a for a in alerts if a.get("Category") == BLOCKING_CATEGORY]):
            return sorted({a["AlertCode"] for a in alerts})
    raise VodSourceAlertError("; ".join(f"{a['AlertCode']}: {a.get('AlertMessage', '')}" for a in blocking))


def _alerts(arn: str) -> list[dict[str, Any]]:
    paginator = aws.client("mediatailor").get_paginator("list_alerts")
    return [alert for page in paginator.paginate(ResourceArn=arn) for alert in page.get("Items", [])]
