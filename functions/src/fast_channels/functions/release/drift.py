"""Drift check of the release module (hourly, and after every release): is every sibling, its Function tree and each
monetized channel's policy still what the Release Controller applied?

For each sibling the controller recorded, it renders the configuration again from that release's bundle (with the
current origin secret) and compares it with ``GetPlaybackConfiguration`` and the logging; it compares each mapped
FunctionId (and its steps) with ``GetFunction``, a demand-origin request (the ad-server route's auction) with the
current origin secret too; and it checks each channel's policy against validator R-C1. Every
difference is logged by path, never by value (a header holds the origin secret), and their number is published as
``ReleaseDrift``; the alarm ``<prefix>-release-drift`` fires on any. Nothing is repaired here: the controller owns
these resources, so drift means someone else changed them.
"""

import json
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from fast_channels.core.monetization import names
from fast_channels.core.monetization.functions import release_tag
from fast_channels.core.monetization.playback import render, render_function
from fast_channels.core.monetization.release import ChannelState, differences
from fast_channels.core.monetization.validators import check_channel_policy
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import is_missing
from fast_channels.functions.release import store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext
    from pydantic import SecretStr

    from fast_channels.core.monetization.bundle import Bundle, Sibling

logger = Logger()
metrics = Metrics()


def _get(operation: Any, **request: Any) -> dict[str, Any] | None:
    try:
        found: dict[str, Any] = operation(**request)
    except ClientError as error:
        if is_missing(error):
            return None
        raise
    return found


def states() -> list[ChannelState]:
    """The recorded states of the channels the module monetizes now (one taken out keeps its item, unchecked)."""
    config = settings()
    config.require("release_table")
    table = aws.table(config.release_table)
    items: list[dict[str, Any]] = []
    request: dict[str, Any] = {"FilterExpression": Attr("pk").begins_with("CHANNEL#"), "ConsistentRead": True}
    while True:
        page = table.scan(**request)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    found = [ChannelState.model_validate_json(item["doc"]) for item in items]
    return [state for state in found if state.channel in config.release_channels]


def sibling_drift(sibling: Sibling, bundle: Bundle, *, release: int, change_id: str, secret: SecretStr) -> list[str]:
    """Paths where the sibling's configuration, logging or Function tree departs from release ``release``."""
    config = settings()
    config.require("prefix")
    put, logging = render(
        sibling.playback, prefix=config.prefix, release=release, change_id=change_id, origin_secret=secret
    )
    mediatailor = aws.client("mediatailor")
    actual = _get(mediatailor.get_playback_configuration, Name=sibling.name)
    if actual is None:
        return [f"{sibling.name}: missing"]
    found = [f"{sibling.name}: {path}" for path in differences(put, actual)]
    wanted_logging = {k: v for k, v in logging.items() if k != "PlaybackConfigurationName"}
    found += [
        f"{sibling.name}: LogConfiguration.{p}" for p in differences(wanted_logging, actual.get("LogConfiguration"))
    ]
    for stem in (sibling.playback.function_mapping or {}).values():
        root = bundle.function(stem)
        tree = [root, *(bundle.function(ref.function_id) for ref in root.steps)] if root else []
        for function in tree:
            if function is None:
                continue
            released = function.released(release)
            http = function.http_request_configuration
            if http is not None and http.demand_origin:  # created with the origin secret (apply.put_functions)
                released = render_function(released, release=release, origin_secret=secret)
            existing = _get(mediatailor.get_function, FunctionId=released.function_id)
            if existing is None:
                found.append(f"{released.function_id}: missing")
                continue
            found += [f"{released.function_id}: {p}" for p in differences(released.put_request(tags=False), existing)]
    return found


def policy_drift(channel: str) -> list[str]:
    """Violations of R-C1 by the channel's live policy."""
    response = _get(aws.client("mediatailor").get_channel_policy, ChannelName=channel)
    if response is None:
        return [f"{channel}: no channel policy"]
    return [f"{channel}: {v.message}" for v in check_channel_policy(json.loads(response["Policy"]), channel=channel)]


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Compare every recorded sibling and channel policy with what the controller applied."""
    logger.set_correlation_id(event.get("scheduled_time") or event.get("change_id"))
    secret = store.secret(store.origin_slot())
    drift: list[str] = []
    checked = 0
    for state in states():
        drift += policy_drift(state.channel)
        for name, record in sorted(state.siblings.items()):
            bundle = store.bundle(names.bundle_key(record.digest))
            sibling = next((s for s in bundle.siblings if s.name == name), None)
            if sibling is None:
                drift.append(f"{name}: not in the bundle of {release_tag(record.release)}")
                continue
            drift += sibling_drift(sibling, bundle, release=record.release, change_id=record.change_id, secret=secret)
            checked += 1
    metrics.add_metric(name="ReleaseDrift", unit=MetricUnit.Count, value=len(drift))
    if drift:
        logger.warning("release drift", extra={"drift": drift})
    else:
        logger.info("no release drift", extra={"siblings": checked})
    return {"siblings": checked, "drift": drift}
