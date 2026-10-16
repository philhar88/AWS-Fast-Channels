"""AgentCore Gateway: the MCP endpoint of the agent tools, authorized by Cognito JWTs and Cedar (SPEC §6)."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from fast_channels.functions.tools.registry import gateway_tool_schema
from fast_channels.infra import nag
from fast_channels.infra.constructs.operator.cedar import gateway_policies
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.nag import acknowledge_references

if TYPE_CHECKING:
    from fast_channels.infra.constructs.operator.tools import ToolTarget

MCP_VERSIONS: Final = (agentcore.MCPProtocolVersion.MCP_2025_03_26, agentcore.MCPProtocolVersion.MCP_2025_06_18)
INSTRUCTIONS: Final = (
    "FAST Channels operations: catalog, media library, Channel Assembly origins, schedules, channels and encoding "
    "ladders. Tools validate and do all arithmetic; approval-gated tools change what airs or what is billed."
)
CONTROL_PLANE_WRITES: Final = tuple(
    f"bedrock-agentcore:{action}"
    for action in (
        "UpdateGateway",
        "DeleteGateway",
        "CreateGatewayTarget",
        "UpdateGatewayTarget",
        "DeleteGatewayTarget",
        "UpdatePolicyEngine",
        "DeletePolicyEngine",
        "CreatePolicy",
        "UpdatePolicy",
        "DeletePolicy",
    )
)
"""Writes that could relax enforcement (``LOG_ONLY``), detach the engine or swap tools (agentcore.md §6.1)."""
_SCHEMA_TYPES: Final = {
    name: getattr(agentcore.SchemaDefinitionType, name.upper())
    for name in ("string", "number", "integer", "boolean", "object", "array")
}


class AgentGateway(Construct):
    """MCP Gateway with one Lambda target per tool target and a Cedar policy engine in ENFORCE mode.

    - Inbound: Cognito access tokens of the console and autopilot clients carrying :data:`AGENT_SCOPE`
      (CUSTOM_JWT). The agent forwards the operator's own token, so Cedar authorizes the human, not the agent.
    - Protocol: MCP without semantic search. The L2 default turns it on; the agent only exposes
      ``<target>___<tool>`` names to the model, and the extra search tool costs per call (agentcore.md §5.1).
    - Tool schemas come from the registry (``gateway_tool_schema``); Cedar policies from
      :func:`~fast_channels.infra.constructs.operator.cedar.gateway_policies`. Policies depend on every target,
      because the engine's Cedar schema is generated from the attached Gateway's tools.

    No role is granted ``UpdateGateway`` or policy writes: whoever holds them can switch enforcement to
    ``LOG_ONLY`` (agentcore.md §6.1).

    Args:
        name: Gateway name (letters, digits, single hyphens).
        policy_engine_name: policy engine name (letters, digits, underscores).
        identity: the Cognito user pool and app clients.
        targets: the Lambda tool targets.
        agent_channel_prefix: name prefix of agent-owned channels (``<prefix>-agent-``).

    Attributes:
        gateway: the Gateway.
        url: its MCP endpoint (``https://<id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp``).
        targets: Gateway targets by target name.
        policy_engine: the Cedar policy engine.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        policy_engine_name: str,
        identity: OperatorIdentity,
        targets: Sequence[ToolTarget],
        agent_channel_prefix: str,
    ) -> None:
        super().__init__(scope, id)
        self.policy_engine = agentcore.PolicyEngine(
            self,
            "Policies",
            policy_engine_name=policy_engine_name,
            description="FAST Channels: which operator group and client may call which agent tool",
        )
        self.gateway = agentcore.Gateway(
            self,
            "Resource",
            gateway_name=name,
            description="FAST Channels agent tools",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_cognito(
                user_pool=identity.user_pool,
                allowed_clients=[identity.console_client, identity.autopilot_client],
                allowed_scopes=[AGENT_SCOPE],
            ),
            protocol_configuration=agentcore.McpProtocolConfiguration(
                instructions=INSTRUCTIONS, supported_versions=list(MCP_VERSIONS)
            ),
            policy_engine_configuration=agentcore.GatewayPolicyEngineConfig(
                policy_engine=self.policy_engine, mode=agentcore.PolicyEngineMode.ENFORCE
            ),
        )
        self.url = cast("str", self.gateway.gateway_url)  # always set on Gateways this L2 creates
        self.targets: Mapping[str, agentcore.GatewayTarget] = {
            target.name: self.gateway.add_lambda_target(
                target.name.title(),
                gateway_target_name=target.name,
                description=target.description,
                lambda_function=target.function,
                tool_schema=agentcore.ToolSchema.from_inline(
                    [_tool_definition(gateway_tool_schema(spec)) for spec in target.tools]
                ),
            )
            for target in targets
        }
        for policy in gateway_policies(
            (spec for target in targets for spec in target.tools),
            gateway_arn=self.gateway.gateway_arn,
            autopilot_client_id=identity.autopilot_client.user_pool_client_id,
            agent_channel_prefix=agent_channel_prefix,
        ):
            resource = self.policy_engine.add_policy(
                policy.name.title().replace("_", ""),
                policy_name=policy.name,
                description=policy.description,
                statement=agentcore.PolicyStatement.from_cedar(policy.statement),
                validation_mode=(
                    agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS
                    if policy.forbid
                    else agentcore.PolicyValidationMode.FAIL_ON_ANY_FINDINGS
                ),
            )
            for target in self.targets.values():
                resource.node.add_dependency(target)
        acknowledge_references(
            self.gateway.role,
            {
                nag.wildcard_resource(f"{target.function.function_arn}:*"): "Gateway L2 grant: invoke the target "
                "function and its versions"
                for target in targets
            },
        )

    @staticmethod
    def protect(*principals: iam.IGrantable) -> None:
        """Explicitly deny ``principals`` every Gateway, target, policy-engine and policy write, so no later grant
        can relax enforcement. The deny covers ``*``: naming this Gateway would make the tool roles depend on the
        Gateway, whose role depends on the tool functions (a cycle)."""
        for principal in principals:
            principal.grant_principal.add_to_principal_policy(
                iam.PolicyStatement(effect=iam.Effect.DENY, actions=list(CONTROL_PLANE_WRITES), resources=["*"])
            )


def _tool_definition(schema: Mapping[str, Any]) -> agentcore.ToolDefinition:
    return agentcore.ToolDefinition(
        name=schema["name"], description=schema["description"], input_schema=_schema(schema["inputSchema"])
    )


def _schema(node: Mapping[str, Any]) -> agentcore.SchemaDefinition:
    """The L2 form of a flattened registry schema (only type/properties/required/items/description)."""
    return agentcore.SchemaDefinition(
        type=_SCHEMA_TYPES[node["type"]],
        description=node.get("description"),
        properties={key: _schema(value) for key, value in node["properties"].items()} if "properties" in node else None,
        required=node.get("required"),
        items=_schema(node["items"]) if "items" in node else None,
    )
