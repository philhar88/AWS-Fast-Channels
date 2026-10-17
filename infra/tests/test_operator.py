"""Operator plane (SPEC §6): constructs in an isolated stage (real DataStack, stand-in media exports)."""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Aspects, Environment, RemovalPolicy, Stack, Stage, Validations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_events as events
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediatailor as mediatailor
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk.assertions import Template
from cdk_nag import AwsSolutionsChecks, ServerlessChecks
from pydantic import BaseModel

from fast_channels.functions.tools.registry import Access, Approval, ToolSpec, all_tools, confirm_tools
from fast_channels.infra.config import FastChannelsConfig
from fast_channels.infra.constructs.alerts import alert_topic
from fast_channels.infra.constructs.functions import (
    FunctionLogGroups,
    LambdaDefaults,
    LogGroupDefaults,
    StateMachineDefaults,
)
from fast_channels.infra.constructs.logs import vended_log_group
from fast_channels.infra.constructs.operator import agent_bundle, cedar
from fast_channels.infra.constructs.operator.console import ConsoleConfig, OperatorConsole, content_security_policy
from fast_channels.infra.constructs.operator.deliveries import LogDelivery, LogType
from fast_channels.infra.constructs.operator.gateway import CONTROL_PLANE_WRITES, INSTRUCTIONS
from fast_channels.infra.constructs.operator.runtime import model_invoke_statements
from fast_channels.infra.constructs.operator.tools import ENVIRONMENT, registry_tools
from fast_channels.infra.exports import ChannelExports, MediaExports
from fast_channels.infra.nag import flatten
from fast_channels.infra.stacks.data import MEMORY_PREFERENCE_NAMESPACE, DataStack
from fast_channels.infra.stacks.operator import OperatorStack

ACCOUNT, REGION = "123456789012", "us-east-1"
CAPTURE = "CAPTURE_PROMPTS"
REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_CONTEXT: dict[str, Any] = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
LADDER = "standard@86bf8088e0"
CDN = "https://d111111abcdef8.cloudfront.net"
DESTINATION = f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-destination:fast-mediatailor"
EVERYTHING = {
    "observability.transaction_search": "enable",
    "agent.admin_email": "admin@example.com",
    "agent.model": "global.anthropic.claude-haiku-5-5",
    "agent.allowed_models": ["global.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5"],
    "channels.iac": [{"name": "fast-demo", "autopilot_cron": "cron(0 */6 * * ? *)"}],
}

type Overrides = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Plane:
    app: App
    data: DataStack
    media: Stack
    operator: OperatorStack

    @property
    def template(self) -> Template:
        return Template.from_stack(self.operator)


def _media_exports(stack: Stack) -> MediaExports:
    """What MediaStack will export, as real resources and cross-stack tokens."""
    config = mediatailor.CfnPlaybackConfiguration(
        stack,
        "Playback",
        name="fast-fast-demo",
        ad_decision_server_url=f"{CDN}/ads/vast.xml",
        video_content_source_url="https://example.com/v1/channel/fast-demo/",
    )
    return MediaExports(
        distribution=cloudfront.Distribution.from_distribution_attributes(
            stack, "Cdn", domain_name="d111111abcdef8.cloudfront.net", distribution_id="E2EXAMPLE"
        ),
        cdn_base_url=CDN,
        content_base_url=CDN,
        event_bus=events.EventBus(stack, "Bus", event_bus_name="fast-fast-channels"),
        alert_topic=alert_topic(stack, "Alerts", name="fast-alerts", email=None),
        ingest_state_machine=sfn.StateMachine.from_state_machine_arn(
            stack, "Ingest", f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:fast-ingest"
        ),
        ladder_provisioner=sfn.StateMachine.from_state_machine_arn(
            stack, "Provisioner", f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:fast-provision-ladder"
        ),
        mediaconvert_queue_arn=f"arn:aws:mediaconvert:{REGION}:{ACCOUNT}:queues/fast",
        source_location_name="fast-media",
        default_ladder_version=LADDER,
        channels=(
            ChannelExports(
                name="fast-demo",
                arn=f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:channel/fast-demo",
                ladder_version=LADDER,
                outputs=("hls",),
                playback_configuration_name="fast-fast-demo",
                hls_prefix=config.attr_hls_configuration_manifest_endpoint_prefix,
            ),
        ),
        playback_configuration_template=json.dumps({"content_base_url": CDN, "log_destination_arn": DESTINATION}),
        mediatailor_log_destination_arn=DESTINATION,
    )


def build(overrides: Overrides | None = None, *, dist: Path, outdir: Path | None = None) -> Plane:
    """An isolated stage with the stage's runtime defaults: Data, a stand-in Media and the Operator stack."""
    context = {
        **CDK_CONTEXT,
        "aws:cdk:bundling-stacks": [],
        **{f"fast-channels.{k}": v for k, v in (overrides or {}).items()},
    }
    app = App(context=context, outdir=str(outdir) if outdir else None)
    config = FastChannelsConfig.from_context(app.node, region=REGION)
    log_groups = LogGroupDefaults(config.observability.log_retention, config.removal_policy)
    stage = Stage(
        app,
        "Test",
        env=Environment(account=ACCOUNT, region=REGION),
        property_injectors=[LambdaDefaults(log_groups), StateMachineDefaults(log_groups)],  # type: ignore[list-item]
    )
    data = DataStack(stage, "Data", config=config)
    media = Stack(stage, "Media")
    operator = OperatorStack(
        stage, "Operator", config=config, data=data.exports, media=_media_exports(media), console_dist=dist
    )
    Aspects.of(stage).add(FunctionLogGroups(log_groups))
    return Plane(app, data, media, operator)


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site = tmp_path_factory.mktemp("dist")
    (site / "index.html").write_text('<!doctype html><script type="module" src="/assets/index-abc.js"></script>')
    (site / "assets").mkdir()
    (site / "assets" / "index-abc.js").write_text("console.log('console');")
    return site


@pytest.fixture(scope="module")
def plane(dist: Path) -> Plane:
    return build(dist=dist)


@pytest.fixture(scope="module")
def template(plane: Plane) -> Template:
    return plane.template


@pytest.fixture(scope="module")
def everything(dist: Path) -> Template:
    return build(EVERYTHING, dist=dist).template


def resources(template: Template, type_: str) -> dict[str, Any]:
    return dict(template.find_resources(type_))


def logical(template: Template, type_: str, prefix: str = "") -> str:
    [found] = [k for k in resources(template, type_) if k.startswith(prefix)]
    return found


def only(template: Template, type_: str) -> dict[str, Any]:
    [found] = resources(template, type_).values()
    return dict(found["Properties"])


def only_runtime(template: Template) -> dict[str, Any]:
    [runtime] = resources(template, "AWS::BedrockAgentCore::Runtime").values()
    return dict(runtime)


def statements(template: Template) -> Iterator[dict[str, Any]]:
    for policy in resources(template, "AWS::IAM::Policy").values():
        yield from policy["Properties"]["PolicyDocument"]["Statement"]


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def role_statements(template: Template, logical_prefix: str) -> list[dict[str, Any]]:
    [policy] = [p for k, p in resources(template, "AWS::IAM::Policy").items() if k.startswith(logical_prefix)]
    return list(policy["Properties"]["PolicyDocument"]["Statement"])


def actions_of(statements_: list[dict[str, Any]], effect: str = "Allow") -> set[str]:
    return {a for s in statements_ if s["Effect"] == effect for a in as_list(s["Action"])}


# --- Cedar --------------------------------------------------------------------------------------------------------


def _policies(*, autopilot: bool = True) -> dict[str, cedar.CedarPolicy]:
    policies = cedar.gateway_policies(
        all_tools(),
        gateway_arn="arn:aws:bedrock-agentcore:us-east-1:1:gateway/g",
        agent_channel_prefix="fast-agent-",
        **({"autopilot_client_id": "auto123", "autopilot_channels": ("fast-demo", "fast-news")} if autopilot else {}),
    )
    return {p.name: p for p in policies}


def _actions(statement: str) -> set[str]:
    return set(__import__("re").findall(r'AgentCore::Action::"([^"]+)"', statement))


def test_cedar_permits_each_access_level_to_its_groups() -> None:
    policies = _policies()
    by_access = {access: {s.qualified_name for s in all_tools() if s.access is access} for access in Access}

    for access, groups in cedar.GROUPS_BY_ACCESS.items():
        policy = policies[f"permit_{access.value}"]
        assert _actions(policy.statement) == by_access[access]
        assert policy.statement.startswith("permit(\n  principal is AgentCore::OAuthUser,")
        assert {g for g in cedar.Group if f'like "*\\"{g}\\"*"' in policy.statement} == set(groups)
        assert not policy.forbid
    assert 'resource == AgentCore::Gateway::"arn:aws:bedrock-agentcore:us-east-1:1:gateway/g"' in policy.statement


def test_cedar_limits_the_autopilot_to_reads_and_scheduling_its_own_channels() -> None:
    policies = _policies()
    reads = {s.qualified_name for s in all_tools() if s.access is Access.READ}
    gated = {s.qualified_name for s in all_tools() if s.approval is Approval.CONFIRM}
    plan, publish = policies["permit_autopilot_plan"].statement, policies["permit_autopilot_publish"].statement

    assert _actions(policies["permit_autopilot"].statement) == reads
    assert all('principal.getTag("client_id") == "auto123"' in p.statement for n, p in policies.items() if "auto" in n)
    assert _actions(plan) == {"schedule___plan"}
    assert plan.endswith(
        'context.input has channel && (context.input.channel like "fast-demo" || context.input.channel like '
        '"fast-news") };'
    )
    assert _actions(publish) == {"schedule___publish"}
    assert 'context.input has plan_id && (context.input.plan_id like "fast-demo.*" ||' in publish
    assert _actions(policies["forbid_autopilot_confirm"].statement) == gated - {"schedule___publish"}
    assert policies["forbid_autopilot_confirm"].forbid


def test_cedar_has_no_autopilot_policies_without_an_autopilot() -> None:
    assert not [name for name in _policies(autopilot=False) if "autopilot" in name]


def test_cedar_machine_client_without_channels_reads_only_and_names_are_safe() -> None:
    def policies(channels: tuple[str, ...]) -> list[cedar.CedarPolicy]:
        return cedar.gateway_policies(
            all_tools(),
            gateway_arn="g",
            agent_channel_prefix="p-",
            autopilot_client_id="a",
            autopilot_channels=channels,
        )

    machine = {policy.name: policy for policy in policies(()) if "autopilot" in policy.name}
    assert set(machine) == {"permit_autopilot", "forbid_autopilot_confirm"}
    assert "schedule___publish" not in machine["permit_autopilot"].statement
    with pytest.raises(ValueError, match="cannot be embedded"):
        policies(('x" || true || "',))


def test_cedar_forbids_destructive_channel_tools_on_iac_channels() -> None:
    policy = _policies()["forbid_iac_channels"]

    assert _actions(policy.statement) == {"channels___delete_channel"}
    assert policy.statement.startswith("forbid(\n  principal,")
    assert policy.statement.endswith(
        'unless { context.input has channel && context.input.channel like "fast-agent-*" };'
    )


def test_cedar_rejects_values_that_would_escape_a_string_literal() -> None:
    with pytest.raises(ValueError, match="cannot be embedded"):
        cedar.gateway_policies(all_tools(), gateway_arn='x" || true', agent_channel_prefix="p-")


def test_cedar_requires_the_autopilot_tools_to_exist() -> None:
    class Empty(BaseModel):
        pass

    lone = ToolSpec("channels", "list_channels", "d", Empty, Empty, Access.READ, Approval.NONE, lambda i, c: Empty())

    with pytest.raises(ValueError, match="autopilot tools"):
        cedar.gateway_policies(
            [lone], gateway_arn="g", agent_channel_prefix="p-", autopilot_client_id="a", autopilot_channels=("c",)
        )


# --- identity -------------------------------------------------------------------------------------------------------


def test_user_pool_is_essentials_admin_invite_only_with_three_groups(template: Template) -> None:
    pool = only(template, "AWS::Cognito::UserPool")

    assert pool["UserPoolTier"] == "ESSENTIALS"
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    assert pool["UsernameAttributes"] == ["email"]
    assert sorted(
        g["Properties"]["GroupName"] for g in resources(template, "AWS::Cognito::UserPoolGroup").values()
    ) == [
        "fast-admins",
        "fast-operators",
        "fast-viewers",
    ]
    template.resource_count_is("AWS::Cognito::UserPoolUser", 0)


def test_managed_login_v2_branding_follows_the_domain(template: Template) -> None:
    [(domain_id, domain)] = resources(template, "AWS::Cognito::UserPoolDomain").items()
    [branding] = resources(template, "AWS::Cognito::ManagedLoginBranding").values()

    assert domain["Properties"]["ManagedLoginVersion"] == 2
    assert domain["Properties"]["Domain"] == f"fast-{ACCOUNT}"
    assert domain_id in branding["DependsOn"]
    assert branding["Properties"]["UseCognitoProvidedValues"] is True


def test_console_client_is_a_pkce_code_flow_client_redirecting_to_the_console(template: Template) -> None:
    [console] = [
        c["Properties"]
        for c in resources(template, "AWS::Cognito::UserPoolClient").values()
        if c["Properties"]["AllowedOAuthFlows"] == ["code"]
    ]

    assert console["GenerateSecret"] is False
    assert console["CallbackURLs"] == console["LogoutURLs"]
    distribution = logical(template, "AWS::CloudFront::Distribution")
    assert console["CallbackURLs"] == [  # no local dev server unless configured
        {"Fn::Join": ["", ["https://", {"Fn::GetAtt": [distribution, "DomainName"]}, "/"]]}
    ]
    assert console["CallbackURLs"][0] == {
        "Fn::Join": ["", ["https://", {"Fn::GetAtt": [distribution, "DomainName"]}, "/"]]
    }
    assert console["AllowedOAuthScopes"][:3] == ["openid", "email", "profile"]
    assert "/agent.invoke" in json.dumps(console["AllowedOAuthScopes"][3])
    assert console["EnableTokenRevocation"] is True
    assert console["RefreshTokenRotation"] == {"Feature": "ENABLED", "RetryGracePeriodSeconds": 30}


def test_console_accepts_a_local_dev_server_only_when_configured(dist: Path) -> None:
    template = build({"agent.console_dev_url": "http://localhost:5173/"}, dist=dist).template
    [console] = [c["Properties"] for c in resources(template, "AWS::Cognito::UserPoolClient").values()]

    assert console["CallbackURLs"][1:] == console["LogoutURLs"][1:] == ["http://localhost:5173/"]


def test_autopilot_client_is_a_confidential_client_credentials_client(everything: Template) -> None:
    [autopilot] = [
        c["Properties"]
        for c in resources(everything, "AWS::Cognito::UserPoolClient").values()
        if c["Properties"]["AllowedOAuthFlows"] == ["client_credentials"]
    ]

    assert autopilot["GenerateSecret"] is True
    assert len(autopilot["AllowedOAuthScopes"]) == 1


def test_no_autopilot_credential_without_an_autopilot(template: Template, named_outputs: Any) -> None:
    clients = resources(template, "AWS::Cognito::UserPoolClient").values()

    assert [c["Properties"]["AllowedOAuthFlows"] for c in clients] == [["code"]]
    assert "AutopilotClientId" not in named_outputs(template)
    for resource in (*resources(template, "AWS::BedrockAgentCore::Gateway").values(), only_runtime(template)):
        assert len(resource["Properties"]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]["AllowedClients"]) == 1
    assert not [p for p in resources(template, "AWS::BedrockAgentCore::Policy").values() if "autopilot" in str(p)]


def test_admin_user_is_created_in_fast_admins(everything: Template) -> None:
    user = only(everything, "AWS::Cognito::UserPoolUser")
    membership = only(everything, "AWS::Cognito::UserPoolUserToGroupAttachment")

    assert user["Username"] == "admin@example.com"
    assert user["DesiredDeliveryMediums"] == ["EMAIL"]
    assert membership["Username"] == {"Ref": logical(everything, "AWS::Cognito::UserPoolUser")}
    assert membership["GroupName"] == {"Ref": logical(everything, "AWS::Cognito::UserPoolGroup", "IdentityAdmins")}


# --- tools ----------------------------------------------------------------------------------------------------------


def tool_functions(template: Template) -> dict[str, dict[str, Any]]:
    return {
        f["Properties"]["Handler"].split(".")[-2]: f["Properties"]
        for f in resources(template, "AWS::Lambda::Function").values()
        if f["Properties"].get("Handler", "").startswith("fast_channels.functions.tools.")
    }


def test_one_function_per_target_with_only_its_environment(template: Template) -> None:
    functions = tool_functions(template)

    assert sorted(functions) == sorted(registry_tools())
    for target, function in functions.items():
        assert function["Handler"] == f"fast_channels.functions.tools.{target}.handler"
        names = set(function["Environment"]["Variables"]) - {"POWERTOOLS_SERVICE_NAME", "POWERTOOLS_METRICS_NAMESPACE"}
        assert names == {"FAST_PREFIX", *ENVIRONMENT[target]}


def test_destructive_mediatailor_actions_only_on_agent_owned_resources(template: Template) -> None:
    destructive = {
        "mediatailor:DeleteChannel",
        "mediatailor:DeleteChannelPolicy",
        "mediatailor:PutChannelPolicy",
        "mediatailor:DeletePlaybackConfiguration",
    }

    for statement in statements(template):
        if statement["Effect"] == "Allow" and destructive & set(as_list(statement["Action"])):
            assert all("-agent-*" in json.dumps(r) for r in as_list(statement["Resource"])), statement


def test_creations_without_resource_permissions_require_the_deployment_tag(template: Template) -> None:
    creations = {
        "mediatailor:CreateChannel",
        "mediatailor:PutPlaybackConfiguration",
        "mediatailor:CreateSourceLocation",
        "mediatailor:CreateVodSource",
        "mediaconvert:CreateJobTemplate",
    }
    found = [s for s in statements(template) if s["Effect"] == "Allow" and creations & set(as_list(s["Action"]))]

    assert {a for s in found for a in as_list(s["Action"])} >= creations
    assert all(s["Condition"] == {"StringEquals": {"aws:RequestTag/fast:app": "fast"}} for s in found)


def test_auto_stop_role_may_only_stop_channels_and_is_passed_by_the_channels_tool(template: Template) -> None:
    [role] = [r for k, r in resources(template, "AWS::IAM::Role").items() if k.startswith("ToolsAutoStopRole")]
    [trust] = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    [pass_role] = [
        s for s in role_statements(template, "ToolsChannelsRoleDefaultPolicy") if s["Action"] == "iam:PassRole"
    ]

    assert trust["Principal"] == {"Service": "scheduler.amazonaws.com"}
    group = logical(template, "AWS::Scheduler::ScheduleGroup", "ToolsAutoStop")
    assert trust["Condition"]["ArnEquals"] == {"aws:SourceArn": {"Fn::GetAtt": [group, "Arn"]}}
    assert actions_of(role_statements(template, "ToolsAutoStopRoleDefaultPolicy")) == {"mediatailor:StopChannel"}
    assert pass_role["Condition"] == {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}


def test_origins_tools_reach_only_this_deployments_source_locations(template: Template) -> None:
    origins = json.dumps(role_statements(template, "ToolsOriginsRoleDefaultPolicy"))

    assert "sourceLocation/fast*" not in origins and "vodSource/fast*/" not in origins
    assert "sourceLocation/fast-*" in origins and "vodSource/fast-*/*" in origins


def test_tables_are_granted_item_access_and_their_one_index(template: Template) -> None:
    for statement in statements(template):
        if any(a.startswith("dynamodb:") for a in as_list(statement["Action"])):
            assert "index/*" not in json.dumps(statement["Resource"])
            assert not any(a.endswith("*") for a in as_list(statement["Action"]))


# --- gateway -------------------------------------------------------------------------------------------------------


def test_gateway_authorizes_cognito_tokens_without_semantic_search(template: Template) -> None:
    gateway = only(template, "AWS::BedrockAgentCore::Gateway")

    assert gateway["AuthorizerType"] == "CUSTOM_JWT"
    jwt = gateway["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["AllowedScopes"] == ["fast-channels/agent.invoke"]
    assert len(jwt["AllowedClients"]) == 1  # the console; the autopilot client only exists with an autopilot
    assert jwt["DiscoveryUrl"]["Fn::Join"][1][-1] == "/.well-known/openid-configuration"
    assert gateway["ProtocolConfiguration"] == {
        "Mcp": {"Instructions": INSTRUCTIONS, "SupportedVersions": ["2025-03-26", "2025-06-18"]}
    }
    assert gateway["PolicyEngineConfiguration"]["Mode"] == "ENFORCE"


def test_gateway_role_is_assumable_only_by_this_accounts_gateways(template: Template) -> None:
    [role] = [r for k, r in resources(template, "AWS::IAM::Role").items() if k.startswith("GatewayServiceRole")]
    gateway = only(template, "AWS::BedrockAgentCore::Gateway")

    assert role["Properties"]["AssumeRolePolicyDocument"]["Statement"] == [
        {
            "Action": "sts:AssumeRole",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/fast-tools-*"},
            },
        }
    ]
    assert gateway["RoleArn"]["Fn::GetAtt"][0].startswith("GatewayServiceRole")
    assert "lambda:InvokeFunction" in actions_of(role_statements(template, "GatewayServiceRoleDefaultPolicy"))


def test_gateway_targets_expose_every_registry_tool_with_correlation_id(template: Template) -> None:
    exposed = {}
    for target in resources(template, "AWS::BedrockAgentCore::GatewayTarget").values():
        name = target["Properties"]["Name"]
        for tool in target["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]:
            exposed[f"{name}___{tool['Name']}"] = tool
            assert "correlation_id" in tool["InputSchema"]["Properties"]

    assert sorted(exposed) == sorted(s.qualified_name for s in all_tools())


def test_policies_depend_on_every_target_and_only_forbids_ignore_findings(template: Template) -> None:
    targets = set(resources(template, "AWS::BedrockAgentCore::GatewayTarget"))
    policies = resources(template, "AWS::BedrockAgentCore::Policy")

    # Policy names are unique per account: the engine name keeps two deployments apart.
    assert sorted(p["Properties"]["Name"] for p in policies.values()) == sorted(
        f"fast_tools_{name}" for name in _policies(autopilot=False)
    )
    for policy in policies.values():
        assert targets <= set(policy["DependsOn"])
        forbid = policy["Properties"]["Name"].startswith("fast_tools_forbid_")
        assert policy["Properties"]["ValidationMode"] == ("IGNORE_ALL_FINDINGS" if forbid else "FAIL_ON_ANY_FINDINGS")


def test_no_role_may_change_the_gateway_or_its_policies(everything: Template) -> None:
    writes = set(CONTROL_PLANE_WRITES)
    for statement in statements(everything):
        if statement["Effect"] == "Allow":
            assert not writes & set(as_list(statement["Action"])), statement
            assert not any(a in {"*", "bedrock-agentcore:*"} for a in as_list(statement["Action"]))


def test_agent_tool_and_autopilot_roles_are_denied_gateway_writes(everything: Template) -> None:
    denied = [
        logical
        for logical, policy in resources(everything, "AWS::IAM::Policy").items()
        if any(
            s["Effect"] == "Deny" and set(as_list(s["Action"])) == set(CONTROL_PLANE_WRITES) and s["Resource"] == "*"
            for s in policy["Properties"]["PolicyDocument"]["Statement"]
        )
    ]

    assert {p.split("RoleDefaultPolicy")[0].removeprefix("Tools") for p in denied if p.startswith("Tools")} == {
        t.title() for t in registry_tools()
    }
    assert any(p.startswith("AgentExecutionRole") for p in denied)
    assert any(p.startswith("AutopilotFunctionRole") for p in denied)


def test_no_public_function_urls_or_open_invoke_permissions(everything: Template) -> None:
    everything.resource_count_is("AWS::Lambda::Url", 0)
    for permission in resources(everything, "AWS::Lambda::Permission").values():
        assert permission["Properties"]["Principal"] != "*"


# --- runtime --------------------------------------------------------------------------------------------------------


def test_runtime_is_an_agui_code_zip_behind_the_cognito_authorizer(template: Template) -> None:
    runtime = only(template, "AWS::BedrockAgentCore::Runtime")

    assert runtime["AgentRuntimeName"] == "fast_scheduler"
    assert runtime["ProtocolConfiguration"] == "AGUI"
    code = runtime["AgentRuntimeArtifact"]["CodeConfiguration"]
    assert (code["Runtime"], code["EntryPoint"]) == ("PYTHON_3_14", ["opentelemetry-instrument", "main.py"])
    jwt = runtime["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert (len(jwt["AllowedClients"]), jwt["AllowedScopes"]) == (1, ["fast-channels/agent.invoke"])
    assert runtime["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}
    assert runtime["LifecycleConfiguration"] == {"IdleRuntimeSessionTimeout": 600, "MaxLifetime": 7200}


def test_runtime_environment_is_the_agent_settings_contract(template: Template, everything: Template) -> None:
    env = only(template, "AWS::BedrockAgentCore::Runtime")["EnvironmentVariables"]
    traced = only(everything, "AWS::BedrockAgentCore::Runtime")["EnvironmentVariables"]

    assert set(env) == {
        "GATEWAY_URL",
        "MEMORY_ID",
        "MEMORY_PREFERENCE_NAMESPACE",
        "CONFIRM_TOOLS",
        "MODEL_SPEC_PARAMETER",
        "DEFAULT_MODEL_ID",
        "ALLOWED_MODELS",
        "OTEL_TRACES_EXPORTER",
        "CAPTURE_PROMPTS",
    }
    assert env["CONFIRM_TOOLS"] == ",".join(confirm_tools())
    assert env["MEMORY_PREFERENCE_NAMESPACE"] == MEMORY_PREFERENCE_NAMESPACE
    assert env["OTEL_TRACES_EXPORTER"] == "none"
    assert env["DEFAULT_MODEL_ID"] in env["ALLOWED_MODELS"].split(",")
    assert "OTEL_TRACES_EXPORTER" not in traced


def test_message_content_stays_out_of_the_logs_unless_evaluated_or_asked_for(
    template: Template, everything: Template, dist: Path
) -> None:
    def captured(t: Template) -> str:
        return str(only(t, "AWS::BedrockAgentCore::Runtime")["EnvironmentVariables"][CAPTURE])

    asked = build({"observability.capture_prompts": True}, dist=dist).template
    declined = build({**EVERYTHING, "observability.capture_prompts": False}, dist=dist).template

    assert (captured(template), captured(everything), captured(asked), captured(declined)) == (
        "false",
        "true",  # online evaluations score the content
        "true",
        "false",
    )


def test_runtime_role_reads_no_object_but_its_code_zip(template: Template) -> None:
    runtime = only(template, "AWS::BedrockAgentCore::Runtime")
    code = runtime["AgentRuntimeArtifact"]["CodeConfiguration"]["Code"]["S3"]
    [deny] = [
        s for s in role_statements(template, "AgentExecutionRoleDefaultPolicy") if s.get("Sid") == "OnlyItsOwnCode"
    ]

    assert (deny["Action"], deny["NotResource"]) == ("s3:GetObject*", f"arn:aws:s3:::{code['Bucket']}/{code['Prefix']}")
    assert "AgentExecutionRoleDefaultPolicy" in only_runtime(template)["DependsOn"][0]


def test_model_spec_parameter_holds_the_default_spec(template: Template) -> None:
    parameter = only(template, "AWS::SSM::Parameter")

    assert parameter["Name"] == "/fast/agent/model-spec"
    assert json.loads(parameter["Value"]) == {
        "model_id": "us.anthropic.claude-haiku-5-5",
        "effort": {"chat": "low", "plan": "medium"},
        "max_tokens": 16000,
    }


def test_global_profiles_get_the_three_statement_grant() -> None:
    rendered = [
        s.to_json()
        for s in model_invoke_statements(
            ["global.anthropic.claude-haiku-5-5"], partition="aws", region="us-east-1", account="1"
        )
    ]

    assert [s["Resource"] for s in rendered] == [
        "arn:aws:bedrock:us-east-1:1:inference-profile/global.anthropic.claude-haiku-5-5",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-5-5",
        "arn:aws:bedrock:::foundation-model/anthropic.claude-haiku-5-5",
    ]
    profile = {"bedrock:InferenceProfileArn": rendered[0]["Resource"]}
    assert rendered[1]["Condition"] == {"StringEquals": profile}
    assert rendered[2]["Condition"] == {"StringEquals": {"aws:RequestedRegion": "unspecified", **profile}}
    assert all(s["Action"] == ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"] for s in rendered)


def test_geo_profiles_reach_their_geography_only_through_the_profile() -> None:
    profile, model = (
        s.to_json()
        for s in model_invoke_statements(
            ["us.anthropic.claude-sonnet-5-5"], partition="aws", region="us-east-1", account="1"
        )
    )

    assert model["Resource"] == "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5-5"
    assert model["Condition"] == {"StringEquals": {"bedrock:InferenceProfileArn": profile["Resource"]}}


def test_runtime_role_reads_the_memory_and_model_spec_but_not_the_gateway(template: Template) -> None:
    allowed = actions_of(role_statements(template, "AgentExecutionRoleDefaultPolicy"))

    assert {
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "ssm:GetParameter",
    } <= allowed
    assert "bedrock-agentcore:InvokeGateway" not in allowed


def test_memory_and_media_are_weak_references_to_their_stacks(plane: Plane) -> None:
    assert {plane.data, plane.media} <= set(plane.operator.dependencies)
    memory_id = json.dumps(plane.template.to_json()["Resources"])
    assert '"Fn::GetStackOutput": {"StackName": "Test-Data"' in memory_id
    assert '"Fn::GetStackOutput": {"StackName": "Test-Media"' in memory_id


def test_runtime_log_group_gets_a_retention_after_the_service_creates_it(template: Template) -> None:
    [retention] = resources(template, "Custom::LogRetention").values()

    assert retention["Properties"]["RetentionInDays"] == 7
    assert "/aws/bedrock-agentcore/runtimes/" in json.dumps(retention["Properties"]["LogGroupName"])
    assert not [
        g
        for g in resources(template, "AWS::Logs::LogGroup").values()
        if "/aws/bedrock-agentcore/runtimes/" in json.dumps(g["Properties"].get("LogGroupName"))
    ]


def test_online_evaluations_only_with_transaction_search(template: Template, everything: Template) -> None:
    template.resource_count_is("AWS::BedrockAgentCore::OnlineEvaluationConfig", 0)
    config = only(everything, "AWS::BedrockAgentCore::OnlineEvaluationConfig")

    assert config["Rule"]["SamplingConfig"]["SamplingPercentage"] == 10
    assert len(config["Evaluators"]) == 3


# --- logs -----------------------------------------------------------------------------------------------------------


def test_agentcore_logs_go_to_scoped_vendedlogs_groups(template: Template) -> None:
    sources = {
        s["Properties"]["Name"]: s["Properties"]["LogType"]
        for s in resources(template, "AWS::Logs::DeliverySource").values()
    }

    assert sources == {
        "fast-gateway-application-logs": "APPLICATION_LOGS",
        "fast-memory-application-logs": "APPLICATION_LOGS",
        "fast-runtime-application-logs": "APPLICATION_LOGS",
        "fast-runtime-usage-logs": "USAGE_LOGS",
    }
    vended = [
        g["Properties"]
        for g in resources(template, "AWS::Logs::LogGroup").values()
        if str(g["Properties"].get("LogGroupName", "")).startswith("/aws/vendedlogs/")
    ]
    assert len(vended) == 4
    for group in vended:
        [statement] = group["ResourcePolicyDocument"]["Statement"]
        resource, _, log_type = (
            group["LogGroupName"].removeprefix("/aws/vendedlogs/bedrock-agentcore/fast/").partition("/")
        )
        own_source = f"fast-{resource}-{log_type.lower().replace('_', '-')}"
        assert statement["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
        assert statement["Resource"].endswith(f"{group['LogGroupName']}:log-stream:*")
        assert statement["Condition"]["ArnLike"] == {  # only its own delivery source, not any in the account
            "aws:SourceArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:delivery-source:{own_source}"
        }
    template.resource_count_is("AWS::Logs::ResourcePolicy", 0)


def test_traces_need_transaction_search(template: Template, everything: Template) -> None:
    template.resource_count_is("AWS::XRay::ResourcePolicy", 0)
    traces = [
        s for s in resources(everything, "AWS::Logs::DeliverySource").values() if s["Properties"]["LogType"] == "TRACES"
    ]
    policy = only(everything, "AWS::XRay::ResourcePolicy")

    assert len(traces) == 3
    assert "delivery.logs.amazonaws.com" in json.dumps(policy["PolicyDocument"])
    assert "delivery-source:fast-*-traces" in json.dumps(policy["PolicyDocument"])
    everything.resource_count_is("AWS::Logs::ResourcePolicy", 0)


def test_every_log_group_has_a_retention(everything: Template) -> None:
    assert all("RetentionInDays" in g["Properties"] for g in resources(everything, "AWS::Logs::LogGroup").values())


def test_vended_log_destinations_must_be_vendedlogs_groups() -> None:
    stack = Stack(App(), "S")

    with pytest.raises(ValueError, match="/aws/vendedlogs/"):
        vended_log_group(
            stack,
            "G",
            name="/fast/logs",
            delivery_sources="fast-*",
            retention=logs.RetentionDays.ONE_DAY,
            removal_policy=RemovalPolicy.DESTROY,
        )
    with pytest.raises(ValueError, match="X-Ray"):
        LogDelivery(stack, "D", name="d", resource_arn="arn", log_type=LogType.APPLICATION_LOGS)


# --- autopilot ------------------------------------------------------------------------------------------------------


def test_autopilot_runs_per_cron_channel_with_one_retry_within_an_hour(everything: Template) -> None:
    schedule = only(everything, "AWS::Scheduler::Schedule")

    assert schedule["ScheduleExpression"] == "cron(0 */6 * * ? *)"
    assert json.loads(schedule["Target"]["Input"]) == {"channel": "fast-demo"}
    assert schedule["Target"]["RetryPolicy"] == {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 1}
    assert "DeadLetterConfig" in schedule["Target"]
    invoke = only(everything, "AWS::Lambda::EventInvokeConfig")
    assert (invoke["MaximumRetryAttempts"], invoke["MaximumEventAgeInSeconds"]) == (1, 3600)
    assert "OnFailure" in invoke["DestinationConfig"]


def test_autopilot_reads_only_its_pool_clients_and_knows_the_runtime(everything: Template) -> None:
    [function] = [
        f["Properties"]
        for f in resources(everything, "AWS::Lambda::Function").values()
        if f["Properties"].get("Handler") == "fast_channels.functions.autopilot.handler"
    ]
    [describe] = [
        s
        for s in role_statements(everything, "AutopilotFunctionRoleDefaultPolicy")
        if s["Action"] == "cognito-idp:DescribeUserPoolClient"
    ]

    assert {
        "RUNTIME_INVOKE_URL",
        "USER_POOL_ID",
        "AUTOPILOT_CLIENT_ID",
        "TOKEN_ENDPOINT",
        "SCOPE",
        "EVENT_BUS",
        "FAST_PREFIX",
    } <= set(function["Environment"]["Variables"])
    assert function["Timeout"] == 900
    assert describe["Resource"] == {"Fn::GetAtt": [logical(everything, "AWS::Cognito::UserPool"), "Arn"]}


def test_autopilot_schedules_assume_their_role_only_from_its_group(everything: Template) -> None:
    [role] = [r for k, r in resources(everything, "AWS::IAM::Role").items() if k.startswith("AutopilotScheduleRole")]
    [trust] = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]

    group = logical(everything, "AWS::Scheduler::ScheduleGroup", "AutopilotGroup")
    assert trust["Condition"] == {
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
        "ArnEquals": {"aws:SourceArn": {"Fn::GetAtt": [group, "Arn"]}},
    }


def test_autopilot_failures_alarm_on_the_media_alert_topic(everything: Template) -> None:
    alarms = {
        a["Properties"]["AlarmName"]: a["Properties"] for a in resources(everything, "AWS::CloudWatch::Alarm").values()
    }
    queue = logical(everything, "AWS::SQS::Queue", "AutopilotDeadLetters")

    assert set(alarms) == {"fast-autopilot-errors", "fast-autopilot-dead-letters"}
    errors, letters = alarms["fast-autopilot-errors"], alarms["fast-autopilot-dead-letters"]
    assert (errors["Namespace"], errors["MetricName"], errors["Threshold"]) == ("AWS/Lambda", "Errors", 1)
    assert (letters["MetricName"], letters["Statistic"], letters["Threshold"]) == (
        "ApproximateNumberOfMessagesVisible",
        "Maximum",
        1,
    )
    assert letters["Dimensions"] == [{"Name": "QueueName", "Value": {"Fn::GetAtt": [queue, "QueueName"]}}]
    for alarm in (errors, letters):
        assert alarm["AlarmActions"] == alarm["OKActions"]
        assert "Fn::GetStackOutput" in json.dumps(alarm["AlarmActions"])  # the Media stack's topic


def test_autopilot_dead_letters_follow_the_removal_policy(dist: Path) -> None:
    template = build({**EVERYTHING, "removal": "retain"}, dist=dist).template
    [queue] = [q for k, q in resources(template, "AWS::SQS::Queue").items() if k.startswith("AutopilotDeadLetters")]

    assert queue["DeletionPolicy"] == "Retain"


def test_no_autopilot_without_crons(template: Template) -> None:
    template.resource_count_is("AWS::Scheduler::Schedule", 0)
    template.resource_count_is("AWS::CloudWatch::Alarm", 0)


# --- console --------------------------------------------------------------------------------------------------------


def test_console_csp_allows_exactly_what_the_console_needs() -> None:
    csp = content_security_policy(region="us-east-1", url_suffix="amazonaws.com", connect_sources=[CDN, CDN])
    directives = dict(d.split(" ", 1) for d in csp.split("; "))

    assert directives["font-src"] == "'self' data:"
    assert directives["media-src"] == "'self' blob: data:"
    assert directives["worker-src"] == "'self' blob:"
    assert directives["connect-src"].split() == [
        "'self'",
        CDN,  # SSAI manifests, segments and /v1/segment ad redirects all come through the CDN
        "https://bedrock-agentcore.us-east-1.amazonaws.com",
        "https://*.gateway.bedrock-agentcore.us-east-1.amazonaws.com",
        "https://cognito-idp.us-east-1.amazonaws.com",
    ]
    assert directives["frame-ancestors"] == "'none'" and directives["object-src"] == "'none'"


def test_console_is_a_private_bucket_behind_cloudfront_oac(template: Template) -> None:
    bucket = only(template, "AWS::S3::Bucket")
    distribution = only(template, "AWS::CloudFront::Distribution")["DistributionConfig"]

    assert set(bucket["PublicAccessBlockConfiguration"].values()) == {True}
    assert distribution["DefaultRootObject"] == "index.html"
    assert distribution["Origins"][0]["OriginAccessControlId"]
    assert distribution["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    headers = only(template, "AWS::CloudFront::ResponseHeadersPolicy")["ResponseHeadersPolicyConfig"][
        "SecurityHeadersConfig"
    ]
    assert "connect-src 'self' https://d111111abcdef8.cloudfront.net" in json.dumps(headers["ContentSecurityPolicy"])
    assert headers["FrameOptions"]["FrameOption"] == "DENY"


def test_console_content_is_deployed_assets_first_then_entry_with_invalidation(template: Template) -> None:
    deployments = resources(template, "Custom::CDKBucketDeployment")
    [(assets_id, assets)] = [(k, d) for k, d in deployments.items() if k.startswith("ConsoleAssets")]
    [entry] = [d for k, d in deployments.items() if k.startswith("ConsoleEntry")]

    assert assets["Properties"]["Exclude"] == ["index.html", "config.json"]
    assert assets["Properties"]["SystemMetadata"] == {"cache-control": "public, max-age=31536000, immutable"}
    assert entry["Properties"]["SystemMetadata"] == {"cache-control": "no-cache"}
    assert entry["Properties"]["DistributionPaths"] == ["/", "/index.html", "/config.json"]
    assert assets_id in entry["DependsOn"]


def test_console_config_follows_contracts_h() -> None:
    document = ConsoleConfig(
        region="us-east-1",
        user_pool_id="pool",
        user_pool_client_id="client",
        cognito_domain="https://fast-1.auth.us-east-1.amazoncognito.com",
        runtime_arn="arn",
        runtime_invoke_url="https://bedrock-agentcore/invocations",
        gateway_url="https://g/mcp",
        cdn_base_url=CDN,
        channels=(
            ChannelExports(
                "fast-demo",
                "arn",
                LADDER,
                ("hls",),
                "fast-fast-demo",
                "https://h.mediatailor.us-east-1.amazonaws.com/v1/master/abc/fast-fast-demo/",
            ),
        ),
    ).document()

    assert set(document) == {
        "region",
        "userPoolId",
        "userPoolClientId",
        "cognitoDomain",
        "runtimeArn",
        "runtimeInvokeUrl",
        "gatewayUrl",
        "cdnBaseUrl",
        "epgBaseUrl",
        "channels",
    }
    assert document["epgBaseUrl"] == f"{CDN}/epg"
    assert document["channels"] == [
        {
            "name": "fast-demo",
            "ladderVersion": LADDER,
            "playback": {"hls": f"{CDN}/v1/master/abc/fast-fast-demo/hls.m3u8"},  # SSAI only
        }
    ]


def test_token_prefixes_are_cut_to_their_path_by_cloudformation(plane: Plane) -> None:
    config = json.dumps(plane.template.to_json())

    assert '"/hls.m3u8\\""' in config
    assert config.count('{"Fn::Select": [6, {"Fn::Split": ["/"') >= 1  # the SSAI URL of the hls output
    assert "insertionMode" not in config  # SSAI only: no GUIDED (SGAI) URLs


def test_missing_console_build_fails_when_bundling(tmp_path: Path) -> None:
    stage = Stage(App(), "S", env=Environment(account=ACCOUNT, region=REGION))
    console = OperatorConsole(
        Stack(stage, "C"), "Console", cognito_domain_url="https://x", media_urls=[CDN], dist=tmp_path / "missing"
    )
    config = ConsoleConfig("r", "p", "c", "d", "a", "u", "g", CDN, ())

    with pytest.raises(FileNotFoundError, match="npm ci && npm run build"):
        console.publish(config)


def test_missing_console_build_is_a_placeholder_when_not_bundling(dist: Path, tmp_path: Path) -> None:
    plane = build(dist=tmp_path / "missing")

    deployments = resources(plane.template, "Custom::CDKBucketDeployment")
    assert [k for k in deployments if k.startswith("ConsoleAssets")] == []


# --- cdk-nag and references --------------------------------------------------------------------------------------


def nag_checked(overrides: Overrides | None, dist: Path, outdir: Path) -> Plane:
    plane = build(overrides, dist=dist, outdir=outdir)
    Validations.of(plane.app).add_plugins(AwsSolutionsChecks(plane.app), ServerlessChecks(plane.app))
    return plane


@pytest.mark.parametrize("overrides", [{}, EVERYTHING], ids=["default", "everything"])
def test_no_unacknowledged_findings(dist: Path, tmp_path: Path, overrides: Overrides) -> None:
    plane = nag_checked(overrides, dist, tmp_path)

    plane.app.synth()  # raises ValidationFailed listing every unacknowledged finding

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []


def test_nag_packs_run_on_the_operator_plane(dist: Path, tmp_path: Path) -> None:
    plane = nag_checked({}, dist, tmp_path)
    sqs.CfnQueue(plane.operator, "Unencrypted")

    with pytest.raises(RuntimeError, match="AwsSolutions-SQS4"):
        plane.app.synth()


@pytest.mark.parametrize(
    ("value", "flat"),
    [
        ("arn:aws:s3:::b/*", "arn:aws:s3:::b/*"),
        ({"Fn::Join": ["", [{"Fn::GetAtt": ["Fn1", "Arn"]}, ":*"]]}, "<Fn1.Arn>:*"),
        ({"Fn::Join": ["", ["arn:", {"Ref": "AWS::Partition"}, ":x"]]}, "arn:<AWS::Partition>:x"),
        (
            {"Fn::GetStackOutput": {"StackName": "S", "OutputName": "O"}},
            '{"Fn::GetStackOutput":{"StackName":"S","OutputName":"O"}}',
        ),
        ("${AWS::Region}", "<AWS::Region>"),
    ],
)
def test_flatten_matches_cdk_nag(value: Any, flat: str) -> None:
    assert flatten(value) == flat


# --- agent code zip -----------------------------------------------------------------------------------------------


def test_agent_zip_has_the_shim_at_its_root_and_the_package(tmp_path: Path) -> None:
    assert agent_bundle.agent_bundling().local.try_bundle(str(tmp_path), None)  # type: ignore[union-attr, arg-type]

    assert (tmp_path / "main.py").read_text() == (REPO_ROOT / "agent" / "main.py").read_text()
    assert (tmp_path / "fast_channels" / "agent" / "main.py").is_file()
    assert (tmp_path / "fast_channels" / "agent" / "skills" / "scheduling" / "SKILL.md").is_file()
    assert (tmp_path / "bin" / "opentelemetry-instrument").stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "main.py").stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / "requirements.txt").exists()
    assert not list(tmp_path.glob("fast_channels/core"))  # the agent does not depend on core


def test_start_channel_may_skip_missed_programs_on_every_deployment_channel(everything: Template) -> None:
    """``channels___start_channel`` deletes missed programs of IaC channels too, and rewrites their guide."""
    statements = [
        s
        for policy in resources(everything, "AWS::IAM::Policy").values()
        if "ToolsChannels" in json.dumps(policy["Properties"]["Roles"])
        for s in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    def allowed(action: str) -> str:
        return json.dumps([s["Resource"] for s in statements if action in json.dumps(s["Action"])])

    assert "program/fast-*/*" in allowed("mediatailor:DeleteProgram")
    assert "epg/*" in allowed("s3:PutObject")
