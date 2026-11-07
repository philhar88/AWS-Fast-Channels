"""The recs module: the Recs stack (``GET /rail``, the rail builder, the cohort schedule), its configuration, and
what it changes elsewhere (the ``recommender`` registry entry, the ``recs`` flag's default, the viewer's
``railUrl``)."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

from fast_channels.core.flags import RecsStrategy
from fast_channels.core.recs import REGISTRY_DEFAULT
from fast_channels.infra.config import FastChannelsConfig
from fast_channels.infra.constructs.viewer import ViewerConfig
from fast_channels.infra.stacks.ops import stage_flags

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
RECS_OFF: dict[str, Any] = {**FASTM, "modules": {**FASTM["modules"], "recs": {"enabled": False}}}


def statements(template: Template) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def role_statements(template: Template, handler: str) -> list[dict[str, Any]]:
    """The statements of the role of the function whose handler is ``handler``."""
    [function] = [
        f for f in template.find_resources("AWS::Lambda::Function").values() if f["Properties"]["Handler"] == handler
    ]
    role = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


@pytest.fixture(scope="module")
def recs(deployment: Callable[..., Any]) -> Template:
    stack = deployment(FASTM).stage.recs
    assert stack is not None and stack.stack_name == "FastChannels-fastm-Recs"
    return Template.from_stack(stack)


def test_the_stack_exists_only_with_the_module(deployment: Callable[..., Any]) -> None:
    assert deployment(RECS_OFF).stage.recs is None
    assert deployment().stage.recs is None  # off in the shipped configuration


def test_get_rail_admits_only_viewer_access_tokens(recs: Template) -> None:
    [authorizer] = recs.find_resources("AWS::ApiGatewayV2::Authorizer").values()
    jwt = authorizer["Properties"]["JwtConfiguration"]
    names = {p["Default"] for p in recs.to_json()["Parameters"].values() if p["Type"].startswith("AWS::SSM::")}
    assert {"/fastm/identity/issuer", "/fastm/identity/client-id"} <= names
    assert authorizer["Properties"]["AuthorizerType"] == "JWT" and len(jwt["Audience"]) == 1
    recs.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {"RouteKey": "GET /rail", "AuthorizationType": "JWT", "AuthorizationScopes": ["fast/playback"]},
    )
    recs.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {"StageName": "$default", "DefaultRouteSettings": {"ThrottlingRateLimit": 50, "ThrottlingBurstLimit": 100}},
    )
    recs.has_resource_properties("AWS::SSM::Parameter", {"Name": "/fastm/recs/rail-url", "Value": Match.any_value()})


def test_the_rail_api_never_invokes_a_model(recs: Template) -> None:
    api = role_statements(recs, "fast_channels.functions.recs.api.handler")

    assert not [s for s in api if "bedrock" in json.dumps(s["Action"])]
    consent = next(s for s in api if s["Action"] == "dynamodb:GetItem" and "fastm-identity" in json.dumps(s))
    assert consent["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["C#*"]}}
    mac = next(s for s in api if s["Action"] == "kms:GenerateMac")
    assert mac["Condition"] == {"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}
    assert any(s["Action"] == "lambda:InvokeFunction" for s in api)
    assert any("appconfig:GetLatestConfiguration" in s["Action"] for s in api)
    recs.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "fast_channels.functions.recs.api.handler", "ReservedConcurrentExecutions": 10},
    )


def test_the_builder_invokes_only_the_recommender_profile_and_counts_only_its_spend(recs: Template) -> None:
    builder = role_statements(recs, "fast_channels.functions.recs.builder.handler")

    invoke = [s for s in builder if "bedrock:InvokeModel" in json.dumps(s["Action"])]
    assert len(invoke) == 2  # the profile, and the foundation model only through it
    through = next(s for s in invoke if "Condition" in s)
    assert "bedrock:InferenceProfileArn" in json.dumps(through["Condition"])
    counter = next(s for s in builder if s["Action"] == "dynamodb:UpdateItem" and "fastm-models" in json.dumps(s))
    assert "COUNTER#recommender#*" in json.dumps(counter["Condition"])
    put = next(s for s in builder if s["Action"] == "s3:PutObject")
    assert "plan/rail/*" in json.dumps(put["Resource"])
    recs.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "fast_channels.functions.recs.builder.handler",
            "Environment": {
                "Variables": Match.object_like(
                    {"MODEL_TASK_PARAMETER": "/fastm/models/tasks/recommender", "RECS_COHORTS": "web-en"}
                )
            },
        },
    )
    recs.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 0})


def test_cohort_rails_are_built_hourly_and_dead_letters_alarm(recs: Template) -> None:
    recs.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {"ScheduleExpression": "rate(1 hour)", "Target": Match.object_like({"Input": '{"cohorts":true}'})},
    )
    recs.has_resource_properties("AWS::CloudWatch::Alarm", {"AlarmName": "fastm-recs-builder-dead-letters"})
    recs.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {"TableName": "fastm-recs", "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True}},
    )


def config_of(deployment: Callable[..., Any], overrides: dict[str, Any]) -> FastChannelsConfig:
    """The validated configuration of a deployment with ``overrides`` (a stage file's)."""
    return FastChannelsConfig.from_context(deployment(overrides).app.node, region="us-east-1")


def test_recs_needs_models_and_identity_and_adds_the_recommender_entry(deployment: Callable[..., Any]) -> None:
    with pytest.raises(ValueError, match="recs needs models and identity"):
        FastChannelsConfig.model_validate({"modules": {"recs": {"enabled": True}}})
    entry = config_of(deployment, FASTM).modules.models.tasks["recommender"]

    assert (entry.model, entry.thinking.value, entry.max_tokens) == ("us.anthropic.claude-haiku-5-5", "disabled", 4000)
    assert entry.eval == REGISTRY_DEFAULT["eval"]
    assert "recommender" not in config_of(deployment, RECS_OFF).modules.models.tasks
    own = {**FASTM["modules"]["models"].get("tasks", {}), "recommender": {**REGISTRY_DEFAULT, "daily_cap_usd": 9}}
    models = {"enabled": True, "tasks": own}
    swapped = config_of(deployment, {**FASTM, "modules": {**FASTM["modules"], "models": models}})
    assert swapped.modules.models.tasks["recommender"].daily_cap_usd == 9


@pytest.mark.parametrize("bad", [["web"], ["WEB-en"], ["web-eng"]])
def test_cohorts_are_device_language(bad: list[str]) -> None:
    with pytest.raises(ValueError, match="cohorts are <device>-<language>"):
        FastChannelsConfig.model_validate({"modules": {"recs": {"cohorts": bad}}})


def test_the_recs_flag_defaults_to_the_recommender_only_with_the_module(deployment: Callable[..., Any]) -> None:
    assert stage_flags(config_of(deployment, FASTM)).recs is RecsStrategy.RECOMMENDER
    assert stage_flags(config_of(deployment, RECS_OFF)).recs is RecsStrategy.POPULARITY


def test_the_viewer_reads_get_rail_only_with_the_module(deployment: Callable[..., Any]) -> None:
    on, off = deployment(FASTM).stage, deployment(RECS_OFF).stage
    assert on.recs in on.viewer.dependencies
    names = {
        p["Default"]
        for p in Template.from_stack(on.viewer).to_json()["Parameters"].values()
        if p["Type"].startswith("AWS::SSM::")
    }
    assert "/fastm/recs/rail-url" in names
    assert "/fastm/recs/rail-url" not in json.dumps(Template.from_stack(off.viewer).to_json())
    base = {
        "region": "us-east-1",
        "user_pool_client_id": "c",
        "playback_url": "https://api.example/v1/playback",
        "cdn_base_url": "https://d1.cloudfront.net",
        "ad_panel_url": None,
        "channels": (),
        "monetized": frozenset(),
        "personas": (),
    }
    assert "railUrl" not in ViewerConfig(**base).document()
    assert (
        ViewerConfig(**base, rail_url="https://rail.example/rail").document()["railUrl"] == "https://rail.example/rail"
    )
