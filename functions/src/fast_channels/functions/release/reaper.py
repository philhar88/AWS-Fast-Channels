"""``Custom::ReleaseReaper``: what the Release Controller created at run time goes with the ``Release`` stack.

CloudFormation does not know the sibling configurations, their vended-log deliveries and delivery sources, the
Functions or the playback plans: the controller created them. When the stack is deleted (the module turned off, or a
stage destroyed), this resource deletes them, owner-checked by the tags ``fast:app=<prefix>`` and
``fast:owner=release-controller``: the siblings first (deliveries, then sources, then the configuration), then the
Functions, executors before the functions they run (a referenced Function cannot be deleted), then each channel's
``plan/playback/<channel>.json``. Create and Update do nothing.
"""

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import FunctionType, function_prefix
from fast_channels.core.monetization.playback import RELEASE_OWNER
from fast_channels.core.tags import TAG_APP, TAG_OWNER
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import MISSING, error_code

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()


def _ignore_missing(operation: Callable[..., Any], **request: Any) -> None:
    try:
        operation(**request)
    except ClientError as error:
        if error_code(error) not in MISSING:
            raise


def _owned(tags: dict[str, str] | None, prefix: str) -> bool:
    return bool(tags) and tags.get(TAG_APP) == prefix and tags.get(TAG_OWNER) == RELEASE_OWNER  # type: ignore[union-attr]


def siblings(prefix: str, channels: list[str]) -> list[str]:
    """The controller's sibling configurations of ``channels``."""
    if not channels:
        return []
    pattern = re.compile(rf"^(?:{'|'.join(map(re.escape, channels))})-p-[a-z0-9]$")
    mediatailor = aws.client("mediatailor")
    return sorted(
        item["Name"]
        for page in mediatailor.get_paginator("list_playback_configurations").paginate()
        for item in page["Items"]
        if pattern.match(item["Name"]) and _owned(item.get("Tags"), prefix)
    )


def delete_sibling(name: str) -> None:
    """A sibling's deliveries and delivery sources, then the configuration."""
    logs = aws.client("logs")
    sources = {
        source["name"]
        for page in logs.get_paginator("describe_delivery_sources").paginate()
        for source in page["deliverySources"]
        if source["name"].startswith(f"{name}-")
    }
    for page in logs.get_paginator("describe_deliveries").paginate():
        for delivery in page["deliveries"]:
            if delivery["deliverySourceName"] in sources:
                _ignore_missing(logs.delete_delivery, id=delivery["id"])
    for source in sorted(sources):
        _ignore_missing(logs.delete_delivery_source, name=source)
    _ignore_missing(aws.client("mediatailor").delete_playback_configuration, Name=name)
    logger.info("sibling deleted", extra={"sibling": name, "delivery_sources": sorted(sources)})


def functions(prefix: str) -> list[dict[str, Any]]:
    """The controller's Functions (``<p>_*``)."""
    start = f"{function_prefix(prefix)}_"
    mediatailor = aws.client("mediatailor")
    return [
        item
        for page in mediatailor.get_paginator("list_functions").paginate()
        for item in page["Items"]
        if item["FunctionId"].startswith(start) and _owned(item.get("Tags"), prefix)
    ]


def reap(prefix: str, channels: list[str], media_bucket: str) -> dict[str, int]:
    """Delete everything the controller created for ``channels``."""
    found = siblings(prefix, channels)
    for name in found:
        delete_sibling(name)
    owned = functions(prefix)
    executors = [f["FunctionId"] for f in owned if f["FunctionType"] == FunctionType.SEQUENTIAL_EXECUTOR]
    leaves = [f["FunctionId"] for f in owned if f["FunctionType"] != FunctionType.SEQUENTIAL_EXECUTOR]
    mediatailor = aws.client("mediatailor")
    for function_id in [*sorted(executors), *sorted(leaves)]:
        _ignore_missing(mediatailor.delete_function, FunctionId=function_id)
    for channel in channels:
        _ignore_missing(aws.client("s3").delete_object, Bucket=media_bucket, Key=names.plan_key(channel))
    counts = {"siblings": len(found), "functions": len(owned)}
    logger.info("release resources reaped", extra=counts)
    return counts


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Delete: reap. Create and Update: nothing."""
    properties = event["ResourceProperties"]
    physical = f"{properties['Prefix']}-release-reaper"
    if event["RequestType"] == "Delete":
        reap(properties["Prefix"], list(properties.get("Channels", [])), properties["MediaBucket"])
    return {"PhysicalResourceId": physical}
