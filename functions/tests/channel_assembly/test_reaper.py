from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from fast_channels.functions.custom_resources import reaper
from fast_channels.functions.custom_resources.reaper import RESOURCE_TYPE, is_complete, on_event

type Stub = Callable[[str], Stubber]
type Event = Callable[..., dict[str, Any]]
type Error = Callable[..., dict[str, Any]]

PREFIX = "fast"
LOCATION = "fast-media"
PROPS = {
    "Prefix": PREFIX,
    "SourceLocation": LOCATION,
    "JobTemplateCategory": PREFIX,
    "TableName": "library",
    "Enabled": "true",
}
OURS = {"fast:app": PREFIX, "fast:owner": "agent"}
ARN = "arn:aws:mediatailor:us-east-1:123456789012"


def delete(cfn_event: Event, **properties: str) -> dict[str, Any]:
    return cfn_event(RESOURCE_TYPE, "Delete", PROPS | properties, physical_id="fast-reaper")


def stack_status(stub: Stub, status: str) -> None:
    stack = {
        "StackName": "FastChannels-Media",
        "CreationTime": datetime(2026, 10, 16, tzinfo=UTC),
        "StackStatus": status,
    }
    stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]}, {"StackName": ANY})


def channel(name: str, tags: dict[str, str]) -> dict[str, Any]:
    return {
        "Arn": f"{ARN}:channel/{name}",
        "ChannelName": name,
        "ChannelState": "RUNNING",
        "Outputs": [],
        "PlaybackMode": "LINEAR",
        "Tier": "BASIC",
        "LogConfiguration": {"LogTypes": []},
        "Tags": tags,
    }


def location(name: str, tags: dict[str, str]) -> dict[str, Any]:
    return {
        "Arn": f"{ARN}:sourceLocation/{name}",
        "SourceLocationName": name,
        "HttpConfiguration": {"BaseUrl": "https://b"},
        "Tags": tags,
    }


def vod_source(location_name: str, name: str) -> dict[str, Any]:
    return {
        "Arn": f"{ARN}:vodSource/{location_name}/{name}",
        "HttpPackageConfigurations": [],
        "SourceLocationName": location_name,
        "VodSourceName": name,
    }


def test_on_event_keeps_one_physical_id(cfn_event: Event, context: Any) -> None:
    create = on_event(cfn_event(RESOURCE_TYPE, "Create", PROPS), context)
    update = on_event(cfn_event(RESOURCE_TYPE, "Update", PROPS, old=PROPS, physical_id="fast-reaper"), context)

    assert create == update == {"PhysicalResourceId": "fast-reaper"}


def test_creates_and_updates_complete_at_once(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("cloudformation")

    assert is_complete(cfn_event(RESOURCE_TYPE, "Create", PROPS), context) == {"IsComplete": True}


def test_retained_deployments_are_never_reaped(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("cloudformation")

    assert is_complete(delete(cfn_event, Enabled="false"), context) == {"IsComplete": True}


@pytest.mark.parametrize("status", ["UPDATE_COMPLETE_CLEANUP_IN_PROGRESS", "UPDATE_ROLLBACK_IN_PROGRESS"])
def test_removing_the_resource_in_an_update_reaps_nothing(
    stub: Stub, cfn_event: Event, context: Any, status: str
) -> None:
    stack_status(stub, status)

    assert is_complete(delete(cfn_event), context) == {"IsComplete": True}


def test_stack_deletion_reaps_agent_channels_vod_sources_locations_and_templates(
    stub: Stub,
    cfn_event: Event,
    context: Any,
    typed: Callable[..., dict[str, Any]],
    client_error: Error,
) -> None:
    mediatailor, logs, mediaconvert, dynamodb = (stub(s) for s in ("mediatailor", "logs", "mediaconvert", "dynamodb"))
    stack_status(stub, "DELETE_IN_PROGRESS")
    agent = "fast-agent-news"
    pc_arn = f"{ARN}:playbackConfiguration/{agent}"
    # 1. agent channels: ours is drained and deleted; another deployment's ("fast-agent-x" of prefix "fast-agent"
    #    is tagged with its own app) and IaC channels are left alone.
    mediatailor.add_response(
        "list_channels",
        {
            "Items": [
                channel(agent, OURS),
                channel("fast-agent-x", {"fast:app": "fast-agent"}),
                channel("fast-demo", {"fast:app": PREFIX}),
            ]
        },
        {},
    )
    mediatailor.add_response("stop_channel", {}, {"ChannelName": agent})
    mediatailor.add_response(
        "describe_channel", {"ChannelState": "STOPPED", "LogConfiguration": {}}, {"ChannelName": agent}
    )
    mediatailor.add_response("get_channel_schedule", {"Items": []}, {"ChannelName": agent, "DurationMinutes": ANY})
    dynamodb.add_response(
        "query",
        {"Items": [typed({"name": "p1"})]},
        {
            "TableName": "library",
            "KeyConditionExpression": ANY,
            "ProjectionExpression": "#n",
            "ExpressionAttributeNames": {"#n": "name"},
        },
    )
    mediatailor.add_response("delete_program", {}, {"ChannelName": agent, "ProgramName": "p1"})
    mediatailor.add_client_error("delete_channel_policy", **client_error("NotFoundException", status=404))
    mediatailor.add_response("get_playback_configuration", {"PlaybackConfigurationArn": pc_arn}, {"Name": agent})
    logs.add_response(
        "describe_delivery_sources",
        {
            "deliverySources": [
                {"name": f"{agent}-ads", "resourceArns": [pc_arn]},
                {"name": "fast-demo-ads", "resourceArns": ["other"]},
            ]
        },
        {},
    )
    logs.add_response(
        "describe_deliveries",
        {
            "deliveries": [
                {"id": "d1", "deliverySourceName": f"{agent}-ads"},
                {"id": "d2", "deliverySourceName": "fast-demo-ads"},
            ]
        },
        {},
    )
    logs.add_response("delete_delivery", {}, {"id": "d1"})
    logs.add_response("delete_delivery_source", {}, {"name": f"{agent}-ads"})
    mediatailor.add_response("delete_playback_configuration", {}, {"Name": agent})
    mediatailor.add_response("delete_channel", {}, {"ChannelName": agent})
    # 2. orphaned agent playback configurations
    mediatailor.add_response(
        "list_playback_configurations",
        {"Items": [{"Name": "fast-agent-old", "Tags": OURS}, {"Name": "fast-demo", "Tags": {"fast:app": PREFIX}}]},
        {},
    )
    mediatailor.add_client_error("get_playback_configuration", **client_error("NotFoundException", status=404))
    # 3. VOD sources: the deployment's location (one locked by orphaned programs) and a runtime location
    runtime = location("fast-byo", OURS)
    locations = {
        "Items": [location(LOCATION, {"fast:app": PREFIX, "fast:owner": "iac"}), runtime, location("other", {})]
    }
    mediatailor.add_response("list_source_locations", locations, {})
    mediatailor.add_response(
        "list_vod_sources",
        {"Items": [vod_source(LOCATION, "slate"), vod_source(LOCATION, "show")]},
        {"SourceLocationName": LOCATION},
    )
    mediatailor.add_response("delete_vod_source", {}, {"SourceLocationName": LOCATION, "VodSourceName": "slate"})
    mediatailor.add_client_error(
        "delete_vod_source",
        **client_error("BadRequestException", "There is at least one program, p9, that refers to this VodSource"),
    )
    mediatailor.add_response(
        "list_vod_sources", {"Items": [vod_source("fast-byo", "film")]}, {"SourceLocationName": "fast-byo"}
    )
    mediatailor.add_client_error("delete_vod_source", **client_error("NotFoundException", status=404))
    # 4. runtime source locations
    mediatailor.add_response("list_source_locations", locations, {})
    mediatailor.add_response("delete_source_location", {}, {"SourceLocationName": "fast-byo"})
    # 5. runtime job templates: only the agent's
    templates: list[dict[str, Any]] = [
        {"Name": n, "Arn": f"arn:aws:mediaconvert:us-east-1:1:jobTemplates/{n}", "Settings": {}}
        for n in ("fast-standard", "fast-agent-lv")
    ]
    mediaconvert.add_response("list_job_templates", {"JobTemplates": templates}, {"Category": PREFIX})
    mediaconvert.add_response(
        "list_tags_for_resource",
        {"ResourceTags": {"Tags": {"fast:app": PREFIX, "fast:owner": "iac"}}},
        {"Arn": templates[0]["Arn"]},
    )
    mediaconvert.add_response("list_tags_for_resource", {"ResourceTags": {"Tags": OURS}}, {"Arn": templates[1]["Arn"]})
    mediaconvert.add_response("delete_job_template", {}, {"Name": "fast-agent-lv"})

    assert is_complete(delete(cfn_event), context) == {"IsComplete": False}  # "show" is still locked


def test_reaping_stops_at_the_time_budget(stub: Stub, cfn_event: Event, context: Any) -> None:
    stack_status(stub, "DELETE_IN_PROGRESS")
    stub("mediatailor")
    context.remaining_ms = reaper.TIME_MARGIN_MS - 1

    assert is_complete(delete(cfn_event), context) == {"IsComplete": False}


def test_a_missing_source_location_has_nothing_to_reap(
    stub: Stub, cfn_event: Event, context: Any, client_error: Error
) -> None:
    stack_status(stub, "ROLLBACK_IN_PROGRESS")
    mediatailor = stub("mediatailor")
    mediatailor.add_response("list_channels", {"Items": []}, {})
    mediatailor.add_response("list_playback_configurations", {"Items": []}, {})
    mediatailor.add_response("list_source_locations", {"Items": []}, {})
    mediatailor.add_client_error("list_vod_sources", **client_error("NotFoundException", status=404))
    mediatailor.add_response("list_source_locations", {"Items": []}, {})
    stub("mediaconvert").add_response("list_job_templates", {"JobTemplates": []}, {"Category": PREFIX})

    assert is_complete(delete(cfn_event), context) == {"IsComplete": True}


def test_unexpected_errors_fail_the_deletion_with_the_aws_error(
    stub: Stub, cfn_event: Event, context: Any, client_error: Error
) -> None:
    stack_status(stub, "DELETE_IN_PROGRESS")
    stub("mediatailor").add_client_error("list_channels", **client_error("AccessDeniedException", status=403))

    with pytest.raises(Exception, match="AccessDeniedException"):
        is_complete(delete(cfn_event), context)
