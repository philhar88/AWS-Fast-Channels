"""The model registry (``modules.models``): the Models stack and the scheduler agent's entry."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk import App, Stack

from fast_channels.core.model_registry import TaskEntry
from fast_channels.infra.constructs.models.registry import ModelRegistry

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

type GetDeployment = Callable[..., Any]

HAIKU, SONNET = "us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5"
RECOMMENDER = {"model": "{geo}.anthropic.claude-haiku-5-5", "effort": "low", "max_tokens": 8000, "data_class": "viewer"}
MODELS = {"modules.models.enabled": True}
TWO_TASKS = {**MODELS, "modules.models.tasks": {"recommender": RECOMMENDER}}
SCHEDULER_PARAMETER = "/fast/models/tasks/scheduler.plan"


def parameters(deployment: Any) -> dict[str, Any]:
    """SSM parameters of the Models stack by name, their values parsed."""
    found = deployment.template(deployment.stage.models).find_resources("AWS::SSM::Parameter")
    return {p["Properties"]["Name"]: json.loads(p["Properties"]["Value"]) for p in found.values()}


def runtime(deployment: Any) -> dict[str, Any]:
    [resource] = (
        deployment.template(deployment.stage.operator).find_resources("AWS::BedrockAgentCore::Runtime").values()
    )
    properties: dict[str, Any] = resource["Properties"]
    return properties


def runtime_statements(deployment: Any) -> list[dict[str, Any]]:
    policies = deployment.template(deployment.stage.operator).find_resources("AWS::IAM::Policy")
    return [
        statement
        for logical_id, policy in policies.items()
        if logical_id.startswith("AgentExecutionRoleDefaultPolicy")
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_no_models_stack_while_the_module_is_off(default: Any) -> None:
    assert default.stage.models is None
    assert "MODEL_TASK_PARAMETER" not in runtime(default)["EnvironmentVariables"]


def test_one_entry_per_task_with_the_regions_models(deployment: GetDeployment) -> None:
    d = deployment(TWO_TASKS)

    assert d.stage.models.stack_name == "FastChannels-Models"
    assert parameters(d) == {
        SCHEDULER_PARAMETER: {
            "model": HAIKU,
            "route": "bedrock",
            "effort": {"chat": "low", "plan": "medium"},
            "thinking": "adaptive",
            "max_tokens": 16000,
            "data_class": "operational",
            "daily_cap_usd": None,
            "eval": None,
        },
        "/fast/models/tasks/recommender": {
            "model": HAIKU,
            "route": "bedrock",
            "effort": "low",
            "thinking": "adaptive",
            "max_tokens": 8000,
            "data_class": "viewer",
            "daily_cap_usd": None,
            "eval": None,
        },
    }
    for document in parameters(d).values():
        TaskEntry.model_validate(document)  # what every reader validates
    [output] = d.template(d.stage.models).find_outputs("TaskParameters").values()
    assert json.loads(output["Value"]) == {
        "recommender": "/fast/models/tasks/recommender",
        "scheduler.plan": SCHEDULER_PARAMETER,
    }


def test_entries_follow_the_region(deployment: GetDeployment) -> None:
    d = deployment({**MODELS, "prefix": "acme", "channels.iac": []}, region="eu-west-1")

    assert parameters(d)["/acme/models/tasks/scheduler.plan"]["model"] == "eu.anthropic.claude-haiku-5-5"
    assert d.stage.models.stack_name == "FastChannels-acme-Models"


def test_the_agent_reads_its_entry_by_name_after_the_registry_is_deployed(deployment: GetDeployment) -> None:
    d = deployment(MODELS)

    assert runtime(d)["EnvironmentVariables"]["MODEL_TASK_PARAMETER"] == SCHEDULER_PARAMETER
    assert {
        "Action": "ssm:GetParameter",
        "Effect": "Allow",
        "Resource": f"arn:aws:ssm:us-east-1:123456789012:parameter{SCHEDULER_PARAMETER}",
    } in runtime_statements(d)
    assert d.stage.models in d.stage.operator.dependencies
    assert "FastChannels-Models" not in json.dumps(d.template(d.stage.operator).to_json())  # no stack reference


def test_the_registry_needs_no_agent(deployment: GetDeployment) -> None:
    d = deployment({**MODELS, "agent.enabled": False})

    assert d.stage.operator is None
    assert set(parameters(d)) == {SCHEDULER_PARAMETER}
    assert d.stage.models.dependencies == []


@pytest.mark.parametrize(
    ("tasks", "allowed", "message"),
    [
        ({"t": TaskEntry(model=SONNET)}, [HAIKU], "models outside allowed_models"),
        ({"t": TaskEntry(model="{geo}.anthropic.claude-haiku-5-5")}, [HAIKU], "resolve {geo} for the region first"),
    ],
)
def test_the_registry_refuses_models_the_deployment_does_not_grant(
    tasks: dict[str, TaskEntry], allowed: list[str], message: str
) -> None:
    with pytest.raises(ValueError, match=message.replace("{", r"\{").replace("}", r"\}")):
        ModelRegistry(Stack(App(), "S"), "Registry", prefix="fast", tasks=tasks, allowed_models=allowed)


def test_models_stack_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion
) -> None:
    d = deployment(TWO_TASKS)

    assert normalized(d.template(d.stage.models)) == snapshot_json
