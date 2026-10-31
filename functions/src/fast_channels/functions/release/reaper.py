"""``Custom::ReleaseReaper``: what the Release Controller created at run time goes with the ``Release`` stack, or with
the channel the module stops monetizing.

CloudFormation does not know the sibling configurations, their vended-log deliveries and delivery sources, the
Functions or the playback plans: the controller created them. Everything here is owner-checked by the tags
``fast:app=<prefix>`` and ``fast:owner=release-controller``.

- Delete (the module turned off, or a stage destroyed): the siblings (prefetch schedules, deliveries, then sources,
  then the configuration), then the Functions, executors before the functions they run (a referenced Function cannot be
  deleted), then each channel's ``plan/playback/<channel>.json``.
- Update that takes channels out of ``modules.release.channels``: their siblings and plans. The Functions stay until
  the stack goes (the other channels' siblings may run them).
- Create: nothing.
"""

import re
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger

from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import FunctionType
from fast_channels.core.monetization.playback import RELEASE_OWNER
from fast_channels.core.tags import TAG_APP, TAG_OWNER
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import delete_deliveries, ignore_missing

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()


def _owned(tags: dict[str, str] | None, prefix: str) -> bool:
    return bool(tags) and tags.get(TAG_APP) == prefix and tags.get(TAG_OWNER) == RELEASE_OWNER  # type: ignore[union-attr]


def siblings(prefix: str, channels: list[str]) -> dict[str, str]:
    """The controller's sibling configurations of ``channels``: name -> ARN."""
    if not channels:
        return {}
    pattern = re.compile(rf"^(?:{'|'.join(map(re.escape, channels))})-p-[a-z0-9]$")
    mediatailor = aws.client("mediatailor")
    return {
        item["Name"]: item["PlaybackConfigurationArn"]
        for page in mediatailor.get_paginator("list_playback_configurations").paginate()
        for item in page["Items"]
        if pattern.match(item["Name"]) and _owned(item.get("Tags"), prefix)
    }


def delete_prefetch_schedules(configuration: str) -> int:
    """Every prefetch schedule of a sibling (the prefetcher's, ``ScheduleType=ALL``): the sibling is the
    controller's, so is everything on it."""
    mediatailor = aws.client("mediatailor")
    names = [
        item["Name"]
        for page in mediatailor.get_paginator("list_prefetch_schedules").paginate(
            PlaybackConfigurationName=configuration, ScheduleType="ALL"
        )
        for item in page.get("Items", [])
    ]
    for name in names:
        ignore_missing(mediatailor.delete_prefetch_schedule, Name=name, PlaybackConfigurationName=configuration)
    return len(names)


def delete_siblings(prefix: str, channels: list[str], media_bucket: str) -> int:
    """The siblings of ``channels`` with their prefetch schedules, deliveries and delivery sources, then the channels'
    plans."""
    found = siblings(prefix, channels)
    mediatailor = aws.client("mediatailor")
    for name, arn in sorted(found.items()):
        if schedules := delete_prefetch_schedules(name):
            logger.info("prefetch schedules deleted", extra={"sibling": name, "schedules": schedules})
        delete_deliveries(arn)
        ignore_missing(mediatailor.delete_playback_configuration, Name=name)
        logger.info("sibling deleted", extra={"sibling": name})
    for channel in channels:
        ignore_missing(aws.client("s3").delete_object, Bucket=media_bucket, Key=names.plan_key(channel))
    return len(found)


def functions(prefix: str) -> list[dict[str, Any]]:
    """The controller's Functions (``<p>_*``)."""
    start = f"{prefix}_"
    mediatailor = aws.client("mediatailor")
    return [
        item
        for page in mediatailor.get_paginator("list_functions").paginate()
        for item in page["Items"]
        if item["FunctionId"].startswith(start) and _owned(item.get("Tags"), prefix)
    ]


def reap(prefix: str, channels: list[str], media_bucket: str) -> dict[str, int]:
    """Delete everything the controller created for ``channels``."""
    deleted = delete_siblings(prefix, channels, media_bucket)
    owned = functions(prefix)
    executors = [f["FunctionId"] for f in owned if f["FunctionType"] == FunctionType.SEQUENTIAL_EXECUTOR]
    leaves = [f["FunctionId"] for f in owned if f["FunctionType"] != FunctionType.SEQUENTIAL_EXECUTOR]
    mediatailor = aws.client("mediatailor")
    for function_id in [*sorted(executors), *sorted(leaves)]:
        ignore_missing(mediatailor.delete_function, FunctionId=function_id)
    counts = {"siblings": deleted, "functions": len(owned)}
    logger.info("release resources reaped", extra=counts)
    return counts


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Delete: reap. Update: the siblings of channels taken out of the module. Create: nothing."""
    properties = event["ResourceProperties"]
    prefix, channels = properties["Prefix"], list(properties.get("Channels", []))
    match event["RequestType"]:
        case "Delete":
            reap(prefix, channels, properties["MediaBucket"])
        case "Update":
            if removed := sorted(set(event.get("OldResourceProperties", {}).get("Channels", [])) - set(channels)):
                count = delete_siblings(prefix, removed, properties["MediaBucket"])
                logger.info("channels no longer monetized", extra={"channels": removed, "siblings": count})
    return {"PhysicalResourceId": f"{prefix}-release-reaper"}
