import json
from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_ssm as ssm

from fast_channels.infra.app import build
from fast_channels.infra.config import ConfigError, FastChannelsConfig
from fast_channels.infra.stage import FastChannelsStage, stage_id

type GetDeployment = Callable[..., Any]

# The shipped us. inference profiles cannot be invoked outside the US (config region checks).
GLOBAL_MODELS = {
    "agent.model": "global.anthropic.claude-haiku-5-5",
    "agent.allowed_models": ["global.anthropic.claude-haiku-5-5"],
}
EVERYTHING = {**GLOBAL_MODELS, "observability.transaction_search": "enable"}


def names(deployment: Any) -> dict[str, str]:
    return {stack.node.id: stack.stack_name for stack in deployment.stacks}


def test_default_stacks(default: Any) -> None:
    assert names(default) == {
        "Data": "FastChannels-Data",
        "Media": "FastChannels-Media",
        "Operator": "FastChannels-Operator",
    }
    assert (default.stage.edge, default.stage.baseline) == (None, None)


def test_operator_stack_only_with_the_agent(deployment: GetDeployment) -> None:
    assert set(names(deployment({"agent.enabled": False}))) == {"Data", "Media"}


def test_edge_stack_in_us_east_1_when_deploying_elsewhere(deployment: GetDeployment) -> None:
    d = deployment(GLOBAL_MODELS, region="eu-west-1")

    assert d.stage.edge is not None
    assert (d.stage.edge.stack_name, d.stage.edge.region) == ("FastChannels-Edge", "us-east-1")
    assert {stack.region for stack in d.stacks if stack is not d.stage.edge} == {"eu-west-1"}


def test_account_baseline_only_when_enabling_transaction_search(deployment: GetDeployment) -> None:
    for mode in ("off", "existing"):
        assert deployment({"observability.transaction_search": mode}).stage.baseline is None
    baseline = deployment({"observability.transaction_search": "enable"}).stage.baseline

    assert baseline is not None
    assert (baseline.stack_name, baseline.termination_protection) == ("FastChannels-AccountBaseline", True)


def test_stack_dependencies(deployment: GetDeployment) -> None:
    stage = deployment(EVERYTHING, region="eu-west-1").stage

    def deps(stack: Stack) -> set[str]:
        return {d.node.id for d in stack.dependencies}

    assert deps(stage.data) == set()
    assert deps(stage.media) == {"Data"}
    assert deps(stage.operator) == {"Data", "Media", "AccountBaseline"}
    assert deps(stage.edge) == {"Data", "Media"}


def test_memory_is_created_before_any_operator_resource(default: Any) -> None:
    assert default.stage.data in default.stage.operator.dependencies
    assert default.stage.data.memory is not None


@pytest.mark.parametrize(("removal", "protected"), [("destroy", False), ("retain", True)])
def test_termination_protection_follows_removal(deployment: GetDeployment, removal: str, protected: bool) -> None:
    d = deployment({**EVERYTHING, "removal": removal}, region="eu-west-1")

    assert {s.node.id: s.termination_protection for s in d.stacks} == {
        "Data": protected,
        "Media": protected,
        "Operator": protected,
        "Edge": protected,
        "AccountBaseline": True,
    }


def test_every_resource_and_stack_is_tagged(default: Any) -> None:
    for stack in default.stacks:
        assert stack.tags.tag_values() == {"fast:app": "fast", "fast:owner": "iac"}
    for bucket in default.template(default.stage.data).find_resources("AWS::S3::Bucket").values():
        assert {"Key": "fast:app", "Value": "fast"} in bucket["Properties"]["Tags"]
        assert {"Key": "fast:owner", "Value": "iac"} in bucket["Properties"]["Tags"]


def test_other_prefixes_get_their_own_stack_names(deployment: GetDeployment) -> None:
    d = deployment({"prefix": "acme", "channels.iac": []})

    assert stage_id("fast") == "FastChannels"
    assert names(d) == {
        "Data": "FastChannels-acme-Data",
        "Media": "FastChannels-acme-Media",
        "Operator": "FastChannels-acme-Operator",
    }
    d.template(d.stage.data).has_resource_properties("AWS::BedrockAgentCore::Memory", {"Name": "acme_memory"})


def test_consumers_get_weak_references_also_across_regions(fresh_deployment: Callable[..., Any]) -> None:
    d = fresh_deployment(GLOBAL_MODELS, region="eu-west-1")
    exports = d.stage.data.exports
    role = iam.Role(d.stage.media, "Consumer", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    exports.media_bucket.grant_read(role)
    exports.library_table.grant_read_write_data(role)
    assert exports.memory is not None
    exports.memory.grant_read(role)
    ssm.StringParameter(d.stage.edge, "LogsBucket", string_value=exports.logs_bucket.bucket_arn)

    media = json.dumps(d.template(d.stage.media).to_json())
    edge = d.template(d.stage.edge).to_json()

    assert "Fn::ImportValue" not in media
    assert media.count('"Fn::GetStackOutput": {"StackName": "FastChannels-Data", "Region": "eu-west-1"') >= 3
    [parameter] = edge["Resources"].values()
    reference = parameter["Properties"]["Value"]["Fn::GetStackOutput"]
    assert (reference["StackName"], reference["Region"]) == ("FastChannels-Data", "eu-west-1")
    assert "BucketPolicy" not in media  # grants never touch DataStack's resource policies


def test_no_account_level_logs_resource_policy_outside_the_baseline(deployment: GetDeployment) -> None:
    d = deployment(EVERYTHING, region="eu-west-1")

    for stack in d.stacks:
        expected = 1 if stack is d.stage.baseline else 0
        d.template(stack).resource_count_is("AWS::Logs::ResourcePolicy", expected)


def test_region_is_required() -> None:
    with pytest.raises(ConfigError, match="cannot determine the deployment region"):
        build(App(), account=None, region=None)
    with pytest.raises(ValueError, match="region must be known at synth time"):
        FastChannelsStage(App(), "Stage", config=FastChannelsConfig(), env=Environment())


def test_unresolved_account_still_synthesizes_across_regions(fresh_deployment: Callable[..., Any]) -> None:
    d = fresh_deployment(GLOBAL_MODELS, region="eu-west-1", account=None)
    ssm.StringParameter(d.stage.edge, "LogsBucket", string_value=d.stage.data.exports.logs_bucket.bucket_arn)

    [parameter] = d.template(d.stage.edge).to_json()["Resources"].values()
    assert parameter["Properties"]["Value"]["Fn::GetStackOutput"]["Region"] == "eu-west-1"
