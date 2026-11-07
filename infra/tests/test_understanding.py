"""The understanding module (M12): its stack, workflow, trigger and grants, and the configuration that enables it."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

from fast_channels.core.understanding.record import TASK
from fast_channels.infra.config import ConfigError, FastChannelsConfig

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
CDK: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "cdk.json").read_text())["context"]
NOVA = "{geo}.amazon.nova-2-lite-v1:0"
DESCRIBE: dict[str, Any] = {
    "model": NOVA,
    "effort": "low",
    "thinking": "disabled",
    "max_tokens": 4000,
    "daily_cap_usd": 5,
    "eval": "amazon.nova-2-lite-v1-0-disabled-4k",  # infra/tests/data/evals
}


def understanding_stage(**module: Any) -> dict[str, Any]:
    """fastm with the understanding module on: its task in the registry and Nova 2 Lite allowed."""
    scheduler = CDK["fast-channels"]["modules"]["models"]["tasks"]["scheduler.plan"]
    allowed = [*CDK["fast-channels"]["agent"]["allowed_models"], NOVA]
    modules = {
        **FASTM["modules"],
        "models": {"enabled": True, "tasks": {"scheduler.plan": scheduler, TASK: DESCRIBE}},
        "understanding": {"enabled": True, **module},
    }
    return {**FASTM, "agent": {**CDK["fast-channels"]["agent"], "allowed_models": allowed}, "modules": modules}


@pytest.fixture(scope="module")
def stack(deployment: Callable[..., Any]) -> Template:
    understanding = deployment(understanding_stage()).stage.understanding
    assert understanding is not None and understanding.stack_name == "FastChannels-fastm-Understanding"
    return Template.from_stack(understanding)


def definition(template: Template) -> dict[str, Any]:
    [machine] = template.find_resources("AWS::StepFunctions::StateMachine").values()
    parts = machine["Properties"]["DefinitionString"]["Fn::Join"][1]
    return json.loads("".join(p if isinstance(p, str) else "0" for p in parts))


def statements(template: Template, role_prefix: str) -> list[dict[str, Any]]:
    found = []
    for name, policy in template.find_resources("AWS::IAM::Policy").items():
        if name.startswith(role_prefix):
            found += policy["Properties"]["PolicyDocument"]["Statement"]
    return found


def actions(statement: dict[str, Any]) -> list[str]:
    value = statement["Action"]
    return value if isinstance(value, list) else [value]


def test_the_module_is_off_by_default(default: Any) -> None:
    assert default.stage.understanding is None
    assert not FastChannelsConfig().modules.understanding.enabled


def test_a_title_is_analysed_on_its_first_content_encode(stack: Template) -> None:
    stack.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["fast-channels.fastm"],
                "detail-type": ["Asset Ready"],
                "detail": {"kind": ["content"]},
            },
            "Targets": [Match.object_like({"DeadLetterConfig": Match.any_value(), "RetryPolicy": Match.any_value()})],
        },
    )
    stack.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        {"StateMachineName": "fastm-understanding", "TracingConfiguration": {"Enabled": True}},
    )
    stack.has_resource_properties(
        "AWS::CloudWatch::Alarm", {"AlarmName": "fastm-understanding-failed", "MetricName": "ExecutionsFailed"}
    )


def test_the_workflow_runs_every_configured_analysis_in_parallel(stack: Template) -> None:
    states = definition(stack)["States"]

    assert set(states) == {"Prepare", "AnyToAnalyse", "Skipped", "Proxy", "Analyses", "Collect", "Describe"}
    chain = [states["Prepare"]["Next"], states["Proxy"]["Next"], states["Analyses"]["Next"], states["Collect"]["Next"]]
    assert chain == ["AnyToAnalyse", "Analyses", "Collect", "Describe"]
    assert states["AnyToAnalyse"]["Default"] == "Proxy"
    branches = [branch["StartAt"] for branch in states["Analyses"]["Branches"]]
    assert branches == ["StartTranscript", "StartSegments", "StartLabels", "StartModeration"]
    segments = states["Analyses"]["Branches"][1]["States"]["StartSegments"]["Arguments"]
    assert segments["SegmentTypes"] == ["SHOT", "TECHNICAL_CUE"]
    transcript = states["Analyses"]["Branches"][0]["States"]["StartTranscript"]["Arguments"]
    assert transcript["IdentifyLanguage"] is True
    assert states["Proxy"]["Resource"].endswith(":mediaconvert:createJob.sync")


def test_only_the_configured_analyses_run(deployment: Callable[..., Any]) -> None:
    stage = deployment(understanding_stage(analyses=["shots", "moderation"])).stage
    states = definition(Template.from_stack(stage.understanding))["States"]

    branches = states["Analyses"]["Branches"]
    assert [branch["StartAt"] for branch in branches] == ["StartSegments", "StartModeration"]
    assert branches[0]["States"]["StartSegments"]["Arguments"]["SegmentTypes"] == ["SHOT"]


def test_the_describe_step_may_invoke_only_its_tasks_profile(stack: Template) -> None:
    found = statements(stack, "WorkflowDescribeRole")
    invoke = [s for s in found if "bedrock:InvokeModel" in actions(s)]

    assert len(invoke) == 2  # the profile, and the foundation model only through it
    assert any("Condition" in s and "bedrock:InferenceProfileArn" in json.dumps(s["Condition"]) for s in invoke)
    assert any(
        s["Resource"] == f"arn:aws:ssm:us-east-1:123456789012:parameter/fastm/models/tasks/{TASK}" for s in found
    )
    [put] = [s for s in found if actions(s) == ["dynamodb:PutItem"]]
    assert put["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["CONTENT#*"]}}
    [count] = [s for s in found if actions(s) == ["dynamodb:UpdateItem"]]
    assert count["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == [f"COUNTER#{TASK}#*"]


def test_mediaconvert_reads_encodes_and_writes_only_proxies(stack: Template) -> None:
    stack.has_resource_properties(
        "AWS::IAM::Role",
        {
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Principal": {"Service": "mediaconvert.amazonaws.com"},
                            "Condition": Match.object_like({"ArnEquals": {"aws:SourceArn": Match.any_value()}}),
                        }
                    )
                ]
            }
        },
    )
    stack.has_resource_properties(
        "AWS::MediaConvert::Queue", {"Name": "fastm-understanding", "PricingPlan": "ON_DEMAND"}
    )
    stack.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": Match.array_with(
                    [
                        Match.object_like({"Prefix": "proxies/", "ExpirationInDays": 1}),
                        Match.object_like({"Prefix": "results/", "ExpirationInDays": 30}),
                    ]
                )
            }
        },
    )


def test_understanding_needs_the_model_registry_and_its_task(load_config: Callable[..., FastChannelsConfig]) -> None:
    stage = understanding_stage()
    without_models = {**stage, "modules": {**stage["modules"], "models": {"enabled": False}}}
    without_task = {
        **stage,
        "modules": {**stage["modules"], "models": {"enabled": True, "tasks": stage["modules"]["models"]["tasks"] | {}}},
    }
    without_task["modules"]["models"]["tasks"] = {
        "scheduler.plan": CDK["fast-channels"]["modules"]["models"]["tasks"]["scheduler.plan"]
    }

    with pytest.raises((ConfigError, ValueError), match="understanding needs models"):
        load_config({"fast-channels": {**CDK["fast-channels"], **without_models}}, region="us-east-1")
    with pytest.raises((ConfigError, ValueError), match=r"understanding\.describe' is not in models\.tasks"):
        load_config({"fast-channels": {**CDK["fast-channels"], **without_task}}, region="us-east-1")
    with pytest.raises(ValueError, match="analyses repeat"):
        FastChannelsConfig.model_validate({"modules": {"understanding": {"analyses": ["shots", "shots"]}}})
