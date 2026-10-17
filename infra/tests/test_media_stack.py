"""The integrated media plane: wiring of MediaStack, its deletion order, its exports and its outputs."""

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import fields
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk import CfnElement, CfnResource, Stack
from aws_cdk.assertions import Match, Template

from fast_channels.core.ladder import Ladder

if TYPE_CHECKING:
    from constructs import IConstruct

    from fast_channels.infra.stacks.media import MediaExports, MediaStack

type GetDeployment = Callable[..., Any]

AGREED_OUTPUTS = {
    "CdnBaseUrl",
    "ContentBaseUrl",
    "EventBusName",
    "IngestStateMachineArn",
    "LadderProvisionerArn",
    "SourceLocationName",
    "DefaultLadderVersion",
    "CaptionEngine",
    "Channels",
    "DashboardUrl",
}
GLOBAL_MODELS = {
    "agent.model": "global.anthropic.claude-haiku-5-5",
    "agent.allowed_models": ["global.anthropic.claude-haiku-5-5"],
}
TWO_CHANNELS: list[dict[str, Any]] = [
    {"name": "fast-demo", "outputs": ["hls", "hls-avc", "dash-avc"]},
    {"name": "fast-uhd", "ladder": "uhd", "outputs": ["hls-hevc"]},
]
TWO_LADDERS = {"ladders.builtin": ["standard", "uhd"], "channels.iac": TWO_CHANNELS}


def logical_id(construct: IConstruct) -> str:
    element = construct if isinstance(construct, CfnElement) else construct.node.default_child
    assert isinstance(element, CfnElement), construct.node.path
    return Stack.of(element).get_logical_id(element)


def cfn_resources(construct: IConstruct) -> list[CfnResource]:
    return [c for c in construct.node.find_all() if isinstance(c, CfnResource)]


def depends_on(template: Mapping[str, Any], construct: IConstruct) -> set[str]:
    value = template["Resources"][logical_id(construct)].get("DependsOn", [])
    return {value} if isinstance(value, str) else set(value)


def references(value: Any) -> Iterator[str]:
    """Every ``Ref``/``Fn::GetAtt`` inside a resolved value, as canonical JSON."""
    match value:
        case {"Ref": _} | {"Fn::GetAtt": _}:
            yield json.dumps(value, sort_keys=True)
        case dict():
            for item in value.values():
                yield from references(item)
        case list():
            for item in value:
                yield from references(item)


def weak_references(value: Any) -> Iterator[dict[str, str]]:
    """Every ``Fn::GetStackOutput`` (``{StackName, Region, OutputName}``) inside a template value."""
    match value:
        case {"Fn::GetStackOutput": reference}:
            yield reference
        case dict():
            for item in value.values():
                yield from weak_references(item)
        case list():
            for item in value:
                yield from weak_references(item)


def export_tokens(exports: MediaExports) -> list[str]:
    """Every value the operator plane can read from ``MediaExports``."""
    strings = [getattr(exports, f.name) for f in fields(exports) if isinstance(getattr(exports, f.name), str)]
    channels = [getattr(c, f.name) for c in exports.channels for f in fields(c) if isinstance(getattr(c, f.name), str)]
    resources = [
        exports.distribution.distribution_id,
        exports.distribution.distribution_domain_name,
        exports.distribution.distribution_arn,
        exports.event_bus.event_bus_name,
        exports.event_bus.event_bus_arn,
        exports.ingest_state_machine.state_machine_arn,
        exports.ladder_provisioner.state_machine_arn,
        exports.alert_topic.topic_arn,
    ]
    return [*strings, *channels, *resources]


@pytest.fixture(scope="module")
def media(default: Any) -> MediaStack:
    stack: MediaStack = default.stage.media
    return stack


@pytest.fixture(scope="module")
def template(media: MediaStack) -> Mapping[str, Any]:
    return Template.from_stack(media).to_json()


# --- Deletion order ------------------------------------------------------------------------------------------------


def test_reaper_runs_before_the_containers_it_empties(media: MediaStack, template: Mapping[str, Any]) -> None:
    destination = media.vended_logs.node.find_child("Destination")

    assert depends_on(template, media.reaper.resource) == {
        logical_id(media.origin.source_location),
        logical_id(media.vended_logs.log_group),
        logical_id(destination),
        logical_id(media.pipeline.queue),
    }


def test_channels_and_the_register_step_are_deleted_before_the_reaper_runs(
    media: MediaStack, template: Mapping[str, Any]
) -> None:
    reaper = logical_id(media.reaper.resource)
    producers = [*media.channels, media.pipeline.register]

    for resource in (r for producer in producers for r in cfn_resources(producer)):
        assert reaper in depends_on(template, resource), resource.node.path


def test_reaper_is_disabled_when_data_is_retained(deployment: GetDeployment) -> None:
    for removal, enabled in (("destroy", True), ("retain", False)):
        media = deployment({"removal": removal}).stage.media
        props = Template.from_stack(media).to_json()["Resources"][logical_id(media.reaper.resource)]["Properties"]
        assert props["Enabled"] is enabled, removal


# --- Ladders, kits and channels ------------------------------------------------------------------------------------


def test_job_template_per_builtin_ladder_but_kits_only_where_used(deployment: GetDeployment) -> None:
    unused = deployment({"ladders.builtin": ["standard", "uhd"]}).stage.media
    used = deployment(TWO_LADDERS).stage.media

    assert (set(unused.ladders), set(unused.kits)) == ({"standard", "uhd"}, {"standard"})
    assert set(used.kits) == {"standard", "uhd"}
    Template.from_stack(unused).resource_count_is("AWS::MediaConvert::JobTemplate", 2)
    Template.from_stack(unused).resource_count_is("Custom::LadderKit", 1)


def test_kits_are_provisioned_into_the_source_location(deployment: GetDeployment) -> None:
    media = deployment(TWO_LADDERS).stage.media
    rendered = Template.from_stack(media).to_json()

    for kit in media.kits.values():
        assert logical_id(media.origin.source_location) in depends_on(rendered, kit.resource)


def test_channels_depend_on_their_ladder_kit_and_use_its_filler(deployment: GetDeployment) -> None:
    media: MediaStack = deployment(TWO_LADDERS).stage.media
    rendered = Template.from_stack(media).to_json()

    for spec, channel in zip(TWO_CHANNELS, media.channels, strict=True):
        ladder = spec.get("ladder", "standard")
        kit = media.kits[ladder]
        props = rendered["Resources"][logical_id(channel.channel)]["Properties"]
        assert logical_id(kit.resource) in depends_on(rendered, channel.channel)
        assert channel.ladder_version == kit.ladder_version == Ladder.builtin(ladder).version
        assert props["FillerSlate"] == {
            "SourceLocationName": media.origin.name,
            "VodSourceName": {"Fn::GetAtt": [logical_id(kit.resource), "FillerVodSource"]},
        }


def test_source_location_is_named_for_the_data_bucket_policy(media: MediaStack) -> None:
    assert media.origin.name == "fast-media"  # DataStack admits exactly sourceLocation/<prefix>-media
    assert media.exports.source_location_name == media.origin.name


# --- Exports and outputs -------------------------------------------------------------------------------------------


def test_exports_describe_the_deployed_media_plane(media: MediaStack) -> None:
    exports = media.exports
    template_json = json.dumps(media.resolve(exports.playback_configuration_template))

    assert exports.default_ladder_version == Ladder.builtin("standard").version
    assert [c.name for c in exports.channels] == ["fast-demo"]
    assert media.resolve(exports.mediatailor_log_destination_arn) == {
        "Fn::GetAtt": [logical_id(media.vended_logs.node.find_child("Destination")), "Arn"]
    }
    assert json.dumps(media.resolve(exports.mediatailor_log_destination_arn)) in template_json
    assert media.resolve(exports.ingest_state_machine.state_machine_arn) == {
        "Ref": logical_id(media.pipeline.state_machine)
    }


def test_operator_stack_consumes_only_media_exports(default: Any) -> None:
    media, operator = default.stage.media, default.stage.operator
    exported = {ref for token in export_tokens(media.exports) for ref in references(media.resolve(token))}
    outputs = Template.from_stack(media).to_json()["Outputs"]
    operator_json = Template.from_stack(operator).to_json()

    used = {r["OutputName"] for r in weak_references(operator_json) if r["StackName"] == media.stack_name}

    assert used, "the operator plane reads the media plane"
    assert "Fn::ImportValue" not in json.dumps(operator_json)
    for name in used:
        assert set(references(outputs[name]["Value"])) <= exported, name


def test_media_outputs(media: MediaStack, named_outputs: Callable[[Template], set[str]]) -> None:
    assert named_outputs(Template.from_stack(media)) == {
        *AGREED_OUTPUTS,
        "DistributionId",
        "PathContract",
        "DashboardName",
    }


def test_channels_output_lists_playback_urls_per_output(template: Mapping[str, Any]) -> None:
    parts = template["Outputs"]["Channels"]["Value"]["Fn::Join"][1]
    skeleton = json.loads("".join(part if isinstance(part, str) else "X" for part in parts))

    assert skeleton == [
        {
            "name": "fast-demo",
            "ladderVersion": Ladder.builtin("standard").version,
            "outputs": ["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"],
            "playback": {
                **{
                    output: {
                        "ssai": f"https://X/X/{output}.m3u8",
                        "sgai": f"https://X/X/{output}.m3u8?aws.insertionMode=GUIDED",
                    }
                    for output in ("hls", "hls-avc", "hls-hevc", "hls-av1")
                },
                "dash-avc": {"dash": "https://X/X/dash-avc.mpd"},
            },
        }
    ]


def test_operator_outputs(default: Any, named_outputs: Callable[[Template], set[str]]) -> None:
    operator = Template.from_stack(default.stage.operator)
    tools = operator.to_json()["Outputs"]["ToolFunctions"]["Value"]["Fn::Join"][1]

    assert named_outputs(operator) == {
        "ConsoleUrl",
        "UserPoolId",
        "ConsoleClientId",
        "CognitoDomain",
        "RuntimeArn",
        "RuntimeInvokeUrl",
        "GatewayUrl",
        "ToolFunctions",
        "ModelSpecParameter",
    }
    assert set(json.loads("".join(p if isinstance(p, str) else "X" for p in tools))) == {
        "catalog",
        "library",
        "origins",
        "schedule",
        "channels",
        "encoding",
    }


def test_autopilot_outputs_its_client_and_alarms_on_the_media_topic(deployment: GetDeployment) -> None:
    stage = deployment(
        {**GLOBAL_MODELS, "channels.iac": [{"name": "fast-demo", "autopilot_cron": "rate(6 hours)"}]}
    ).stage
    operator = Template.from_stack(stage.operator).to_json()
    media = stage.media
    topic = {r["OutputName"] for r in weak_references(operator["Resources"]) if r["StackName"] == media.stack_name}

    assert "AutopilotClientId" in operator["Outputs"]
    assert any(
        media.resolve(media.exports.alert_topic.topic_arn)
        == Template.from_stack(media).to_json()["Outputs"][name]["Value"]
        for name in topic
    )


def test_ingest_dead_letters_raise_an_alarm(media: MediaStack, template: Mapping[str, Any]) -> None:
    alarm = media.observability.alarms["fast-ingest-dead-letters"]
    properties = template["Resources"][logical_id(alarm)]["Properties"]

    assert (properties["MetricName"], properties["Statistic"], properties["Threshold"]) == (
        "ApproximateNumberOfMessagesVisible",
        "Maximum",
        1,
    )
    assert properties["Dimensions"] == [
        {"Name": "QueueName", "Value": {"Fn::GetAtt": [logical_id(media.pipeline.dead_letter_queue), "QueueName"]}}
    ]
    assert properties["AlarmActions"] == [{"Ref": logical_id(media.observability.topic)}]


def test_media_exports_carry_no_dead_fields(media: MediaStack) -> None:
    assert not hasattr(media.exports, "mediaconvert_role_arn")  # read by no consumer


# --- CloudFront logs -----------------------------------------------------------------------------------------------


def test_cdn_access_logs_live_in_media_in_us_east_1_and_in_edge_elsewhere(deployment: GetDeployment) -> None:
    local = deployment().stage
    remote = deployment(GLOBAL_MODELS, region="eu-west-1").stage

    assert local.media.access_logs is not None and local.edge is None
    assert remote.media.access_logs is None and remote.edge is not None
    Template.from_stack(local.media).resource_count_is("AWS::Logs::DeliverySource", 5)  # CDN, bus, 3 MediaTailor
    Template.from_stack(remote.media).resource_count_is("AWS::Logs::DeliverySource", 4)
    [source] = Template.from_stack(remote.edge).find_resources("AWS::Logs::DeliverySource").values()
    [reference] = weak_references(source["Properties"]["ResourceArn"])
    outputs = Template.from_stack(remote.media).to_json()["Outputs"]
    assert (reference["StackName"], reference["Region"]) == ("FastChannels-Media", "eu-west-1")
    assert outputs[reference["OutputName"]]["Value"] == {"Ref": logical_id(remote.media.distribution)}


@pytest.mark.parametrize("price_class", ["PriceClass_All", "PriceClass_200", "PriceClass_100"])
def test_distribution_uses_the_configured_price_class(deployment: GetDeployment, price_class: str) -> None:
    media = deployment({"delivery.price_class": price_class}).stage.media

    Template.from_stack(media).has_resource_properties(
        "AWS::CloudFront::Distribution", {"DistributionConfig": Match.object_like({"PriceClass": price_class})}
    )


def test_every_provider_framework_function_logs_at_info(template: Mapping[str, Any]) -> None:
    framework = {
        logical: f["Properties"]["LoggingConfig"]["ApplicationLogLevel"]
        for logical, f in template["Resources"].items()
        if f["Type"] == "AWS::Lambda::Function" and "framework" in logical
    }

    assert len(framework) == 8  # kit (3), reaper (3), channel and as-run providers (1 each)
    assert set(framework.values()) == {"INFO"}


def test_machine_client_without_an_autopilot(deployment: GetDeployment) -> None:
    """``agent.machine_client`` creates the machine client (integrations, e2e) without any autopilot schedule."""
    stage = deployment({**GLOBAL_MODELS, "agent.machine_client": True}).stage
    operator = Template.from_stack(stage.operator)

    assert "AutopilotClientId" in operator.to_json()["Outputs"]
    operator.resource_count_is("AWS::Scheduler::Schedule", 0)
