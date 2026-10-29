"""The model registry (``modules.models``): profiles, entries and counters in the Models stack, and the scheduler
agent as the ``scheduler.plan`` task's caller."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk import App, Stack
from aws_cdk import aws_iam as iam

from fast_channels.core.model_registry import TaskEntry, TaskSettings
from fast_channels.infra.constructs.models.profiles import profile_name, source_arn, task_invoke_statements
from fast_channels.infra.constructs.models.registry import ModelRegistry
from fast_channels.infra.exports import TaskProfile

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

type GetDeployment = Callable[..., Any]

HAIKU, SONNET = "us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5"
RECOMMENDER = {
    "model": "{geo}.anthropic.claude-haiku-5-5",
    "effort": "low",
    "max_tokens": 8000,
    "data_class": "viewer",
    "eval": "haiku-5-5-adaptive-8k",  # a fixture record (infra/tests/data/evals)
}
MODELS = {"modules.models.enabled": True}
TWO_TASKS = {**MODELS, "modules.models.tasks": {"recommender": RECOMMENDER}}
BUDGETS = {**MODELS, "modules.models.budgets_action": True, "notifications.email": "ops@example.com"}
SCHEDULER_PARAMETER = "/fast/models/tasks/scheduler.plan"
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/"
INVOKE = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]


def _text(value: Any) -> str:
    """A template string value, with each ``Fn::GetAtt`` of a profile as ``<profile:<logical id>>``."""
    match value:
        case str():
            return value
        case {"Fn::Join": ["", parts]}:
            return "".join(_text(part) for part in parts)
        case {"Fn::GetAtt": [logical_id, "InferenceProfileArn"]}:
            return f"<profile:{logical_id}>"
    raise AssertionError(f"unexpected template value {value!r}")


def parameters(deployment: Any) -> dict[str, Any]:
    """SSM parameters of the Models stack by name, their values parsed."""
    found = deployment.template(deployment.stage.models).find_resources("AWS::SSM::Parameter")
    return {p["Properties"]["Name"]: json.loads(_text(p["Properties"]["Value"])) for p in found.values()}


def profiles(deployment: Any) -> dict[str, dict[str, Any]]:
    """Application inference profiles of the Models stack by logical id."""
    found = deployment.template(deployment.stage.models).find_resources("AWS::Bedrock::ApplicationInferenceProfile")
    return {logical_id: resource["Properties"] for logical_id, resource in found.items()}


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


def profile_output(deployment: Any, logical_id: str) -> dict[str, Any]:
    """The weak reference the Operator stack takes to a profile's ARN."""
    stack = deployment.stage.models
    [name] = [
        name
        for name, output in deployment.template(stack).find_outputs("*").items()
        if output["Value"] == {"Fn::GetAtt": [logical_id, "InferenceProfileArn"]} and name.startswith("PublishOutput")
    ]
    return {"Fn::GetStackOutput": {"StackName": stack.stack_name, "Region": "us-east-1", "OutputName": name}}


def test_no_models_stack_while_the_module_is_off(default: Any) -> None:
    environment = runtime(default)["EnvironmentVariables"]

    assert default.stage.models is None
    assert not {"MODEL_TASK_PARAMETER", "MODEL_PROFILE_ARN", "MODEL_COUNTER_TABLE"} & set(environment)


def test_one_profile_per_task_copied_from_its_models_system_profile(deployment: GetDeployment) -> None:
    d = deployment(TWO_TASKS)

    assert sorted(profiles(d).values(), key=lambda p: p["InferenceProfileName"]) == [
        {
            "Description": f"FAST Channels fast model task {task}",
            "InferenceProfileName": name,
            "ModelSource": {"CopyFrom": f"arn:aws:bedrock:us-east-1:123456789012:inference-profile/{HAIKU}"},
            "Tags": [
                {"Key": "fast:app", "Value": "fast"},
                {"Key": "fast:owner", "Value": "iac"},
                {"Key": "fast:task", "Value": task},
            ],
        }
        for task, name in (("recommender", "fast-recommender"), ("scheduler.plan", "fast-scheduler-plan"))
    ]


def test_one_entry_per_task_naming_its_own_profile(deployment: GetDeployment) -> None:
    d = deployment(TWO_TASKS)
    [scheduler] = [lid for lid, p in profiles(d).items() if p["InferenceProfileName"] == "fast-scheduler-plan"]
    [recommender] = [lid for lid, p in profiles(d).items() if p["InferenceProfileName"] == "fast-recommender"]

    assert d.stage.models.stack_name == "FastChannels-Models"
    assert parameters(d) == {
        SCHEDULER_PARAMETER: {
            "model": HAIKU,
            "route": "bedrock",
            "effort": {"chat": "low", "plan": "medium"},
            "thinking": "adaptive",
            "max_tokens": 16000,
            "data_class": "operational",
            "daily_cap_usd": 5.0,
            "eval": "haiku-5-5-adaptive-16k",
            "profile_arn": f"<profile:{scheduler}>",
        },
        "/fast/models/tasks/recommender": {
            "model": HAIKU,
            "route": "bedrock",
            "effort": "low",
            "thinking": "adaptive",
            "max_tokens": 8000,
            "data_class": "viewer",
            "daily_cap_usd": None,
            "eval": "haiku-5-5-adaptive-8k",
            "profile_arn": f"<profile:{recommender}>",
        },
    }
    for document in parameters(d).values():  # what every reader validates, once CloudFormation resolved the ARN
        TaskEntry.model_validate({**document, "profile_arn": f"{PROFILE}abc123"})
    outputs = d.template(d.stage.models).find_outputs("*")
    assert json.loads(outputs["TaskParameters"]["Value"]) == {
        "recommender": "/fast/models/tasks/recommender",
        "scheduler.plan": SCHEDULER_PARAMETER,
    }
    assert json.loads(_text(outputs["TaskProfiles"]["Value"])) == {
        "recommender": f"<profile:{recommender}>",
        "scheduler.plan": f"<profile:{scheduler}>",
    }


def test_counters_table(deployment: GetDeployment) -> None:
    template = deployment(MODELS).template(deployment(MODELS).stage.models)

    template.has_resource_properties(
        "AWS::DynamoDB::GlobalTable",
        {
            "TableName": "fast-models",
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}],
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
        },
    )


def test_entries_follow_the_region(deployment: GetDeployment) -> None:
    d = deployment({**MODELS, "prefix": "acme", "channels.iac": []}, region="eu-west-1")

    assert parameters(d)["/acme/models/tasks/scheduler.plan"]["model"] == "eu.anthropic.claude-haiku-5-5"
    assert d.stage.models.stack_name == "FastChannels-acme-Models"
    [profile] = profiles(d).values()
    assert profile["ModelSource"]["CopyFrom"].endswith(":inference-profile/eu.anthropic.claude-haiku-5-5")
    assert profile["InferenceProfileName"] == "acme-scheduler-plan"


def test_the_agent_invokes_only_its_own_profile(deployment: GetDeployment) -> None:
    d = deployment(TWO_TASKS)
    [scheduler] = [lid for lid, p in profiles(d).items() if p["InferenceProfileName"] == "fast-scheduler-plan"]
    own = profile_output(d, scheduler)
    invokes = [s for s in runtime_statements(d) if s["Action"] == INVOKE]

    assert invokes == [
        {"Action": INVOKE, "Effect": "Allow", "Resource": own},
        {
            "Action": INVOKE,
            "Condition": {"StringEquals": {"bedrock:InferenceProfileArn": own}},
            "Effect": "Allow",
            "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5",
        },
    ]  # no system profile, no other model, not the recommender's profile
    environment = runtime(d)["EnvironmentVariables"]
    assert environment["MODEL_PROFILE_ARN"] == own
    assert (environment["DEFAULT_MODEL_ID"], environment["ALLOWED_MODELS"]) == (HAIKU, HAIKU)


def test_the_agent_reads_its_entry_and_counts_its_spend_by_name(deployment: GetDeployment) -> None:
    d = deployment(MODELS)
    environment = runtime(d)["EnvironmentVariables"]
    statements = runtime_statements(d)

    assert environment["MODEL_TASK_PARAMETER"] == SCHEDULER_PARAMETER
    assert environment["MODEL_COUNTER_TABLE"] == "fast-models"
    assert environment["ALERT_TOPIC_ARN"] == "arn:aws:sns:us-east-1:123456789012:fast-alerts"  # by name
    assert {
        "Action": "ssm:GetParameter",
        "Effect": "Allow",
        "Resource": f"arn:aws:ssm:us-east-1:123456789012:parameter{SCHEDULER_PARAMETER}",
    } in statements
    assert {
        "Action": "dynamodb:UpdateItem",
        "Condition": {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["COUNTER#scheduler.plan#*"]}},
        "Effect": "Allow",
        "Resource": "arn:aws:dynamodb:us-east-1:123456789012:table/fast-models",
    } in statements
    assert {"Action": "sns:Publish", "Effect": "Allow", "Resource": environment["ALERT_TOPIC_ARN"]} in statements
    assert d.stage.models in d.stage.operator.dependencies
    media = d.template(d.stage.media).find_outputs("*")
    assert not [name for name in media if name.startswith("PublishOutputRefObservabilityTopic")]  # Media unchanged


def test_the_entry_as_deployed_is_the_agents_fallback(deployment: GetDeployment) -> None:
    d = deployment(MODELS)
    environment = runtime(d)["EnvironmentVariables"]
    [scheduler] = profiles(d)

    deployed = environment["MODEL_TASK_DEFAULT"]["Fn::Join"][1]
    assert json.loads(deployed[0] + "<profile>" + deployed[2]) == {
        **parameters(d)[SCHEDULER_PARAMETER],
        "profile_arn": "<profile>",
    }
    assert deployed[1] == profile_output(d, scheduler)


def test_with_the_registry_the_model_spec_is_gone(default: Any, deployment: GetDeployment) -> None:
    d = deployment(MODELS)
    operator = d.template(d.stage.operator)

    assert "MODEL_SPEC_PARAMETER" not in runtime(d)["EnvironmentVariables"]
    assert not [p for p in operator.find_resources("AWS::SSM::Parameter").values() if "model-spec" in str(p)]
    assert "ModelSpecParameter" not in operator.find_outputs("*")
    assert "ModelSpecParameter" in default.template(default.stage.operator).find_outputs("*")  # v3.0.0 keeps it


def test_without_a_cap_there_is_no_budget(deployment: GetDeployment) -> None:
    # As the CLI passes it: CDK context drops a None inside an object.
    d = deployment({**BUDGETS, "modules.models.tasks": '{"scheduler.plan": {"daily_cap_usd": null}}'})

    assert d.stage.operator.budget is None


def test_the_registry_needs_no_agent(deployment: GetDeployment) -> None:
    d = deployment({**MODELS, "agent.enabled": False})

    assert d.stage.operator is None
    assert set(parameters(d)) == {SCHEDULER_PARAMETER}
    assert d.stage.models.dependencies == []


def test_budgets_action_denies_only_the_task_roles_profile(deployment: GetDeployment) -> None:
    d = deployment(BUDGETS)
    template = d.template(d.stage.operator)
    [scheduler] = profiles(d)
    own = profile_output(d, scheduler)

    [budget] = template.find_resources("AWS::Budgets::Budget").values()
    assert budget["Properties"]["Budget"] == {
        "BudgetLimit": {"Amount": 155, "Unit": "USD"},  # 31 days of the $5 daily cap
        "BudgetName": "fast-scheduler-plan",
        "BudgetType": "COST",
        "FilterExpression": {"Tags": {"Key": "fast:task", "MatchOptions": ["EQUALS"], "Values": ["scheduler.plan"]}},
        "TimeUnit": "MONTHLY",  # Budgets actions refuse daily budgets
    }
    [action] = template.find_resources("AWS::Budgets::BudgetsAction").values()
    properties = action["Properties"]
    assert (properties["ActionType"], properties["ApprovalModel"], properties["NotificationType"]) == (
        "APPLY_IAM_POLICY",
        "AUTOMATIC",
        "ACTUAL",
    )
    assert properties["ActionThreshold"] == {"Type": "PERCENTAGE", "Value": 100}
    assert properties["Subscribers"] == [{"Address": "ops@example.com", "Type": "EMAIL"}]
    [role] = properties["Definition"]["IamActionDefinition"]["Roles"]
    assert role["Ref"].startswith("AgentExecutionRole")
    [deny] = template.find_resources("AWS::IAM::ManagedPolicy").values()
    assert deny["Properties"]["PolicyDocument"]["Statement"] == [{"Action": INVOKE, "Effect": "Deny", "Resource": own}]
    [execution] = [
        r
        for r in template.find_resources("AWS::IAM::Role").values()
        if r["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]
        == {"Service": "budgets.amazonaws.com"}
    ]
    assert execution["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": "123456789012"}
    }
    assert d.stage.operator.budget is not None


def test_no_budget_without_the_flag(deployment: GetDeployment) -> None:
    d = deployment(MODELS)

    d.template(d.stage.operator).resource_count_is("AWS::Budgets::BudgetsAction", 0)


@pytest.mark.parametrize(
    ("prefix", "task", "name"),
    [("fast", "scheduler.plan", "fast-scheduler-plan"), ("fastci-pr7", "ops_triage.x", "fastci-pr7-ops-triage-x")],
)
def test_profile_names(prefix: str, task: str, name: str) -> None:
    assert profile_name(prefix, task) == name


def test_profile_names_stay_within_the_limit() -> None:
    with pytest.raises(ValueError, match="longer than 64"):
        profile_name("fast", "a" * 60)


def test_profiles_copy_system_profiles_or_foundation_models() -> None:
    stack = Stack(App(), "S", env={"account": "123456789012", "region": "eu-west-1"})

    def joined(rest: str) -> dict[str, Any]:
        return {"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, rest]]}

    assert stack.resolve(source_arn(stack, "global.anthropic.claude-haiku-5-5")) == joined(
        ":bedrock:eu-west-1:123456789012:inference-profile/global.anthropic.claude-haiku-5-5"
    )
    assert stack.resolve(source_arn(stack, "anthropic.claude-haiku-5-5")) == joined(
        ":bedrock:eu-west-1::foundation-model/anthropic.claude-haiku-5-5"
    )


@pytest.mark.parametrize(
    ("model", "resources"),
    [
        (HAIKU, ["arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5"]),
        (
            "global.anthropic.claude-haiku-5-5",
            [
                "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5",
                "arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-5-5",
            ],
        ),
        ("anthropic.claude-haiku-5-5", ["arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-5-5"]),
    ],
)
def test_task_invoke_statements(model: str, resources: list[str]) -> None:
    profile = TaskProfile(task="t", model=model, arn=f"{PROFILE}abc")

    own, through = (s.to_json() for s in task_invoke_statements(profile, partition="aws"))

    assert own == {"Action": INVOKE, "Effect": "Allow", "Resource": f"{PROFILE}abc"}
    assert through["Resource"] == (resources if len(resources) > 1 else resources[0])
    assert through["Condition"] == {"StringEquals": {"bedrock:InferenceProfileArn": f"{PROFILE}abc"}}
    assert all(isinstance(s, iam.PolicyStatement) for s in task_invoke_statements(profile, partition="aws"))


@pytest.mark.parametrize(
    ("tasks", "profiles", "allowed", "message"),
    [
        ({"t": TaskSettings(model=SONNET)}, {"t": "p"}, [HAIKU], "models outside allowed_models"),
        (
            {"t": TaskSettings(model="{geo}.anthropic.claude-haiku-5-5")},
            {"t": "p"},
            [HAIKU],
            "resolve {geo} for the region first",
        ),
        ({"t": TaskSettings(model=HAIKU)}, {}, [HAIKU], "tasks without an inference profile: ['t']"),
    ],
)
def test_the_registry_refuses_what_the_deployment_does_not_grant(
    tasks: dict[str, TaskSettings], profiles: dict[str, str], allowed: list[str], message: str
) -> None:
    escaped = message.replace("{", r"\{").replace("}", r"\}").replace("[", r"\[").replace("]", r"\]")
    with pytest.raises(ValueError, match=escaped):
        ModelRegistry(
            Stack(App(), "S"), "Registry", prefix="fast", tasks=tasks, profiles=profiles, allowed_models=allowed
        )


def test_models_stack_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion
) -> None:
    d = deployment(TWO_TASKS)

    assert normalized(d.template(d.stage.models)) == snapshot_json


def test_the_module_changes_only_the_models_and_operator_stacks(
    default: Any, deployment: GetDeployment, normalized: Callable[..., Any]
) -> None:
    d = deployment(MODELS)

    for stack in ("data", "media"):
        assert normalized(d.template(getattr(d.stage, stack))) == normalized(
            default.template(getattr(default.stage, stack))
        ), stack
