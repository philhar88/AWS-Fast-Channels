"""An agent channel, created and deleted with the channels tools as the agent calls them (docs/channel-assembly.md,
"Agent channels").

``channels___create_channel`` builds a STOPPED LINEAR channel with AS_RUN logs, its STITCHED_ONLY playback
configuration with one vended-log delivery per log type, and a channel policy that admits only that configuration,
each tagged with the deployment. ``channels___delete_channel`` removes all of them and the channel's media library
items. The channel is never started, so the module bills nothing by the hour; it leaves the IaC channel alone.

Configuring the channel's and the playback configuration's logs requires ``iam:CreateServiceLinkedRole`` on
MediaTailor's service-linked role, even when the role exists. The channels tool role of the first v3.0.0 tag lacked
it, so agent channels could not be created, and no test created one.
"""

import json
from collections.abc import Callable, Iterator
from functools import partial
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import pytest
from _stack import Plane
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from harness import ToolError

from fast_channels.core.tags import TAG_APP, TAG_CORRELATION, TAG_LADDER, TAG_OWNER, Owner
from fast_channels.functions.channel_ops import is_missing
from fast_channels.functions.tools.channels import PlaybackTemplate
from fast_channels.functions.tools.library_store import channel_key

if TYPE_CHECKING:
    from _stack import Stage
    from harness import Channel, Tools
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_mediatailor import MediaTailorClient

pytestmark = pytest.mark.e2e

SHORT_NAME: Final = f"e2e-{uuid4().hex[:6]}"
"""The tool's ``name``, new per run: the channel is ``<prefix>-agent-e2e-<hex>``."""


@pytest.fixture(scope="module")
def mediatailor(stage: Stage) -> MediaTailorClient:
    return stage.session.client("mediatailor", region_name=stage.region)


@pytest.fixture(scope="module")
def logs(stage: Stage) -> CloudWatchLogsClient:
    return stage.session.client("logs", region_name=stage.region)


@pytest.fixture(scope="module")
def template(stage: Stage) -> PlaybackTemplate:
    """What every agent channel's playback configuration shares: the channels function's
    ``PLAYBACK_CONFIG_TEMPLATE``."""
    function = stage.json_output(Plane.OPERATOR, "ToolFunctions")["channels"]
    configuration = stage.session.client("lambda", region_name=stage.region).get_function_configuration(
        FunctionName=function
    )
    return PlaybackTemplate.model_validate_json(configuration["Environment"]["Variables"]["PLAYBACK_CONFIG_TEMPLATE"])


@pytest.fixture(scope="module")
def created(tools: Tools, channel: Channel) -> Iterator[dict[str, Any]]:
    """``channels___create_channel`` on the IaC channel's ladder (READY, ``test_03_ladder_kit``). Whatever fails, the
    channel is deleted at the end of the module if ``test_delete_channel_leaves_nothing`` did not delete it."""
    detail = tools("channels___create_channel", name=SHORT_NAME, ladder_version=channel.ladder_version)
    try:
        yield detail
    finally:
        try:
            tools("channels___delete_channel", channel=detail["name"])
        except ToolError as error:
            if error.code != "NOT_FOUND":
                raise


@pytest.fixture(scope="module")
def tags(stage: Stage, channel: Channel, run_id: str) -> dict[str, str]:
    """The tags of every resource the tool creates (CONTRACTS §E); the stage's reaper deletes by them."""
    return {TAG_APP: stage.prefix, TAG_OWNER: Owner.AGENT, TAG_LADDER: channel.ladder_version, TAG_CORRELATION: run_id}


def tagged(resource: Any, tags: dict[str, str]) -> bool:
    return tags.items() <= dict(resource or {}).items()


def missing(call: Callable[[], object]) -> bool:
    """Whether ``call()`` fails because its resource does not exist (other errors are raised)."""
    try:
        call()
    except ClientError as error:
        if is_missing(error):
            return True
        raise
    return False


def deliveries_from(logs: CloudWatchLogsClient, sources: set[str]) -> dict[str, list[dict[str, Any]]]:
    """The account's deliveries from each of ``sources`` (in the stage's region), by source name."""
    found: dict[str, list[dict[str, Any]]] = {}
    for page in logs.get_paginator("describe_deliveries").paginate():
        for delivery in page.get("deliveries", []):
            if (source := delivery.get("deliverySourceName", "")) in sources:
                found.setdefault(source, []).append(dict(delivery))
    return found


def test_create_channel_returns_a_stopped_agent_channel(
    stage: Stage, tools: Tools, channel: Channel, created: dict[str, Any]
) -> None:
    name = f"{stage.prefix}-agent-{SHORT_NAME}"
    cdn = stage.output(Plane.MEDIA, "CdnBaseUrl").rstrip("/")

    assert created["name"] == created["playback_configuration"] == name, created
    assert (created["state"], created["owner"], created["ladder_version"]) == (
        "STOPPED",
        "agent",
        channel.ladder_version,
    ), created
    assert set(created["playback"]) == set(created["outputs"]) == set(channel.outputs), created
    for output, url in created["playback"].items():
        assert url.startswith(cdn) and url.endswith(f"/{output}.m3u8"), created
    listed = {c["name"]: c["state"] for c in tools("channels___list_channels", owner="agent")["channels"]}
    assert listed.get(name) == "STOPPED", listed


def test_channel_and_playback_configuration_are_tagged_logged_and_paired(
    mediatailor: MediaTailorClient, template: PlaybackTemplate, created: dict[str, Any], tags: dict[str, str]
) -> None:
    name = created["name"]
    described = mediatailor.describe_channel(ChannelName=name)
    configuration = mediatailor.get_playback_configuration(Name=name)
    [statement] = json.loads(mediatailor.get_channel_policy(ChannelName=name)["Policy"])["Statement"]

    assert described["ChannelState"] == "STOPPED" and tagged(described.get("Tags"), tags), described
    assert described.get("LogConfiguration", {}).get("LogTypes") == ["AS_RUN"], described
    assert configuration.get("InsertionMode") == "STITCHED_ONLY", configuration
    assert tagged(configuration.get("Tags"), tags), configuration.get("Tags")
    logging = configuration.get("LogConfiguration", {})
    assert logging.get("EnabledLoggingStrategies") == ["VENDED_LOGS"], logging
    assert logging.get("PercentEnabled") == template.log_percent, logging
    # Only this channel's own playback configuration may read the channel (core.specs.channel_policy).
    assert statement["Resource"] == described["Arn"], statement
    assert statement["Condition"]["ArnLike"] == {"aws:SourceArn": configuration["PlaybackConfigurationArn"]}


def test_every_log_type_is_delivered_and_tagged(
    mediatailor: MediaTailorClient,
    logs: CloudWatchLogsClient,
    template: PlaybackTemplate,
    created: dict[str, Any],
    tags: dict[str, str],
) -> None:
    configuration = mediatailor.get_playback_configuration(Name=created["name"])["PlaybackConfigurationArn"]
    sources = template.delivery_sources(created["name"])
    deliveries = deliveries_from(logs, set(sources.values()))

    for log_type, name in sources.items():
        source = logs.get_delivery_source(name=name)["deliverySource"]
        assert (source.get("logType"), source.get("resourceArns")) == (log_type, [configuration]), source
        assert tagged(logs.list_tags_for_resource(resourceArn=source["arn"]).get("tags"), tags), source
        assert len(deliveries.get(name, [])) == 1, f"{name} needs exactly one delivery: {deliveries}"
        [delivery] = deliveries[name]
        assert delivery.get("deliveryDestinationArn") == template.log_destination_arn, delivery
        assert tagged(logs.list_tags_for_resource(resourceArn=delivery["arn"]).get("tags"), tags), delivery


def test_delete_channel_leaves_nothing(
    stage: Stage,
    tools: Tools,
    mediatailor: MediaTailorClient,
    logs: CloudWatchLogsClient,
    library: Table,
    template: PlaybackTemplate,
    created: dict[str, Any],
) -> None:
    name = created["name"]
    sources = set(template.delivery_sources(name).values())
    s3 = stage.session.client("s3", region_name=stage.region)

    deleted = tools("channels___delete_channel", channel=name)

    assert (deleted["channel"], deleted["state"], deleted["programs_deleted"]) == (name, "DELETED", 0), deleted
    assert missing(partial(mediatailor.describe_channel, ChannelName=name))
    assert missing(partial(mediatailor.get_channel_policy, ChannelName=name))
    assert missing(partial(mediatailor.get_playback_configuration, Name=name))
    assert [s for s in sorted(sources) if not missing(partial(logs.get_delivery_source, name=s))] == []
    assert deliveries_from(logs, sources) == {}
    assert library.query(KeyConditionExpression=Key("pk").eq(channel_key(name)["pk"]))["Items"] == []
    assert name not in {c["name"] for c in tools("channels___list_channels")["channels"]}
    feeds = s3.list_objects_v2(Bucket=stage.output(Plane.DATA, "MediaBucketName"), Prefix=f"epg/{name}.")
    assert feeds.get("KeyCount", 0) == 0, feeds.get("Contents")
