"""The scheduler agent on AgentCore Runtime: code zip, AG-UI protocol, Cognito JWT inbound (SPEC §6)."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from fast_channels.core.model_registry import task_parameter
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.config import GEO_PLACEHOLDER
from fast_channels.infra.constructs.models.caps import counter_table_name, grant_counting
from fast_channels.infra.constructs.models.profiles import foundation_model, task_invoke_statements
from fast_channels.infra.constructs.models.registry import READ_ACTIONS, entry_arn
from fast_channels.infra.constructs.operator.agent_bundle import agent_artifact, agent_asset
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.nag import acknowledge_references, file_asset_objects

if TYPE_CHECKING:
    from fast_channels.infra.exports import TaskProfile

IDLE_TIMEOUT: Final = Duration.minutes(10)
MAX_LIFETIME: Final = Duration.hours(2)
FORWARDED_HEADERS: Final = ("Authorization",)
"""The operator's bearer token reaches the agent, which forwards it to the Gateway so Cedar sees the operator."""
DEFAULT_EFFORT: Final = {"chat": "low", "plan": "medium"}  # models-ui.md §2.5
DEFAULT_MAX_TOKENS: Final = 16_000
EVALUATORS: Final = (
    agentcore.BuiltinEvaluator.GOAL_SUCCESS_RATE,
    agentcore.BuiltinEvaluator.TOOL_SELECTION_ACCURACY,
    agentcore.BuiltinEvaluator.TOOL_PARAMETER_ACCURACY,
)
EVALUATION_SAMPLING_PERCENT: Final = 10
INVOKE_ACTIONS: Final = ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream")
"""Strands streams with ConverseStream, which needs the streaming action too (models-ui.md §1.3)."""
CAPTURE_PROMPTS: Final = "CAPTURE_PROMPTS"
"""The agent's content switch (``fast_channels.agent.telemetry``): unless ``true`` it turns off ADOT's default
message capture (``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT``) and redacts content on Strands spans."""
_PROFILE_PREFIXES: Final = frozenset({"global", "us", "eu", "apac", "ca", "jp", "au", "us-gov"})


@dataclass(frozen=True, slots=True)
class RegistryTask:
    """The agent's task in the model registry, when the registry is deployed (``modules.models``)."""

    name: str
    """The task (``scheduler.plan``)."""
    profile: TaskProfile
    """Its model and application inference profile."""
    daily_cap_usd: float | None
    """Its daily spend cap (``null``: none)."""
    alert_topic_arn: str | None
    """Told once a day when the cap is reached (``None``: nobody)."""


class SchedulerAgent(Construct):
    """The ``fast_channels.agent`` app as an AgentCore Runtime.

    - Code zip of the agent package (:func:`~.agent_bundle.agent_asset`), ``PYTHON_3_14``, ADOT entry point,
      ProtocolType ``AGUI`` (verified live in stage 1, ADR-0010).
    - Inbound: Cognito access tokens of the identity's clients (console, autopilot if any) with
      :data:`AGENT_SCOPE`; the ``Authorization`` header is forwarded to the code. Sessions idle out after 10 min and
      end after 2 h.
    - Model: a ``ModelSpec`` JSON SSM parameter (switchable without a redeploy) whose model must be in
      ``allowed_models``; the role may invoke exactly those models (3-statement pattern for ``global.`` profiles).
      With ``model_task`` (the model registry) the agent reads that task's registry entry first and the ModelSpec
      only when the entry is missing or invalid, invokes only the task's application inference profile (the role
      may invoke that profile and its model only through it), and counts the task's daily spend against its cap.
    - Memory: read and write of the AgentCore Memory (events and preference records).
    - Code: the role reads its own code zip and no other object: the L2 grants read on the whole CDK assets bucket
      (every CDK app's code in the account), so a deny covers everything else.
    - Message content (prompts, tool results, replies) is kept out of the runtime's OpenTelemetry logs and spans
      unless ``record_prompts`` (``CAPTURE_PROMPTS``).
    - The service creates ``/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`` with the runtime and without a
      retention; a ``LogRetention`` resource sets it (and deletes the group with the stack unless retained).

    Args:
        runtime_name: Runtime name (letters, digits, underscores).
        identity: Cognito user pool and app clients.
        gateway_url: MCP endpoint of the AgentCore Gateway.
        memory: the AgentCore Memory; ``memory_preference_namespace`` is its USER_PREFERENCE namespace template.
        model: default model id (DEFAULT_MODEL_ID and the initial ModelSpec).
        allowed_models: model ids the ModelSpec may name.
        confirm_tools: qualified names of tools that need operator approval.
        prefix: deployment prefix (the SSM parameter path).
        traces: CloudWatch Transaction Search is on: spans are exported and :data:`EVALUATORS` score 10 % of the
            sessions online (evaluations read spans); otherwise ADOT exports no spans.
        log_retention, removal_policy: of the runtime log group.
        record_prompts: keep message content in the runtime's logs (``otel-rt-logs``) and spans (config
            ``observability.capture_prompts``); readable by anyone with ``logs:GetLogEvents`` on the group.
        model_task: the agent's task in the model registry when the registry is deployed (``modules.models``);
            ``model`` must then be the task's model and ``allowed_models`` exactly that model.

    Attributes:
        runtime: the Runtime; ``invoke_url`` is its HTTPS invocation endpoint (DEFAULT qualifier).
        model_spec: the ModelSpec parameter.
        log_group_retention: the ``LogRetention`` of the runtime's service-created log group.
        evaluation: the online evaluation configuration (``None`` without traces).

    Raises:
        ValueError: ``model`` is not in ``allowed_models``, a model id still holds ``{geo}``, or ``model_task``'s
            model is not ``model`` and ``allowed_models``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        runtime_name: str,
        identity: OperatorIdentity,
        gateway_url: str,
        memory: agentcore.IMemory,
        memory_preference_namespace: str,
        model: str,
        allowed_models: Sequence[str],
        confirm_tools: Sequence[str],
        prefix: str,
        traces: bool,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        record_prompts: bool = False,
        model_task: RegistryTask | None = None,
    ) -> None:
        super().__init__(scope, id)
        if model not in allowed_models:
            raise ValueError(f"default model {model!r} is not in allowed_models")
        if unresolved := [m for m in allowed_models if GEO_PLACEHOLDER in m]:
            raise ValueError(f"{unresolved}: resolve {{geo}} for the region first (FastChannelsConfig.for_region)")
        if model_task and (model_task.profile.model, tuple(allowed_models)) != (model, (model,)):
            raise ValueError(f"with model task {model_task.name}, the model and allowed_models are its model only")
        self.model_spec = ssm.StringParameter(
            self,
            "ModelSpec",
            parameter_name=f"/{prefix}/agent/model-spec",
            description="Scheduler agent ModelSpec: model_id (one of ALLOWED_MODELS), effort per mode, max_tokens",
            string_value=json.dumps({"model_id": model, "effort": DEFAULT_EFFORT, "max_tokens": DEFAULT_MAX_TOKENS}),
        )
        environment = {
            "GATEWAY_URL": gateway_url,
            "MEMORY_ID": memory.memory_id,
            "MEMORY_PREFERENCE_NAMESPACE": memory_preference_namespace,
            "CONFIRM_TOOLS": ",".join(confirm_tools),
            "MODEL_SPEC_PARAMETER": self.model_spec.parameter_name,
            **(_registry_environment(prefix, model_task) if model_task else {}),
            "DEFAULT_MODEL_ID": model,
            "ALLOWED_MODELS": ",".join(allowed_models),
            CAPTURE_PROMPTS: "true" if record_prompts else "false",
        }
        if not traces:
            # Without Transaction Search the X-Ray OTLP endpoint rejects every span batch (HTTP 400, stage-1 spike);
            # ADOT only sets a default, and OTel maps "none" to no exporter (aws-opentelemetry-distro 0.21 source).
            environment["OTEL_TRACES_EXPORTER"] = "none"
        code = agent_asset(self, "Code")
        self.runtime = agentcore.Runtime(
            self,
            "Resource",
            runtime_name=runtime_name,
            description="FAST Channels scheduling agent (AG-UI)",
            agent_runtime_artifact=agent_artifact(code),
            protocol_configuration=agentcore.ProtocolType.AGUI,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                identity.user_pool, identity.clients, allowed_scopes=[AGENT_SCOPE]
            ),
            request_header_configuration=agentcore.RequestHeaderConfiguration(
                allowlisted_headers=list(FORWARDED_HEADERS)
            ),
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=IDLE_TIMEOUT, max_lifetime=MAX_LIFETIME
            ),
            environment_variables=environment,
        )
        # AgentCore creates the endpoint's log group together with the runtime (CreateLogGroup with no retention;
        # pre-creating it fails with AlreadyExists, observed), so its retention is set afterwards.
        self.log_group_retention = logs.LogRetention(
            self,
            "Logs",
            log_group_name=f"/aws/bedrock-agentcore/runtimes/{self.runtime.agent_runtime_id}-DEFAULT",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        stack = Stack.of(self)
        self._acknowledge_log_retention_provider(stack)
        statements = (
            task_invoke_statements(model_task.profile, partition=stack.partition)
            if model_task
            else model_invoke_statements(
                allowed_models, partition=stack.partition, region=stack.region, account=stack.account
            )
        )
        for statement in statements:
            self.runtime.add_to_role_policy(statement)
        self.model_spec.grant_read(self.runtime)
        if model_task:
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    actions=list(READ_ACTIONS), resources=[entry_arn(self, prefix=prefix, task=model_task.name)]
                )
            )
            grant_counting(self.runtime, self, prefix=prefix, task=model_task.name)
            if model_task.alert_topic_arn is not None:
                self.runtime.add_to_role_policy(
                    iam.PolicyStatement(actions=["sns:Publish"], resources=[model_task.alert_topic_arn])
                )
        memory.grant_read(self.runtime)
        memory.grant_write(self.runtime)
        self.evaluation: agentcore.OnlineEvaluationConfig | None = None
        if traces:
            self.evaluation = agentcore.OnlineEvaluationConfig(
                self,
                "Evaluation",
                online_evaluation_config_name=f"{runtime_name}_online",
                description="Goal success and tool use of a sample of scheduler agent sessions",
                evaluators=[agentcore.EvaluatorSelector.builtin(evaluator) for evaluator in EVALUATORS],
                data_source=agentcore.DataSourceConfig.from_agent_runtime_endpoint(self.runtime),
                sampling_percentage=EVALUATION_SAMPLING_PERCENT,
            )
            # Unified span destination: AgentCore lets X-Ray write spans to the agent's own log group
            # (AgentCore devguide, observability-configure and runtime-permissions).
            self.runtime.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["logs:PutResourcePolicy"],
                    resources=[
                        arn(self, "logs", "log-group", f"/aws/bedrock-agentcore/runtimes/{runtime_name}-*", colon=True)
                    ],
                )
            )
        wildcards = _role_wildcards(stack, allowed_models, runtime_name, traces)
        if model_task:
            family = foundation_model(model_task.profile.model)
            wildcards[nag.wildcard_resource(f"arn:{stack.partition}:bedrock:*::foundation-model/{family}")] = (
                f"Application inference profile of model task {model_task.name}: routes to the Regions of the "
                "profile it copies; allowed only through the task's profile (bedrock:InferenceProfileArn)"
            )
        acknowledge_references(self.runtime.role, wildcards)
        if self.evaluation is not None and self.evaluation.execution_role is not None:
            acknowledge_references(self.evaluation.execution_role, _evaluation_wildcards(stack, self.runtime))
        self._read_only(code)

    def _read_only(self, code: s3_assets.Asset) -> None:
        """Deny reading any S3 object but the runtime's code zip ``code``: the L2 grants ``s3:GetObject*`` on the
        whole CDK assets bucket. The Runtime depends on its role's policy, so a new zip is readable before it is
        fetched."""
        self.runtime.add_to_role_policy(
            iam.PolicyStatement(
                sid="OnlyItsOwnCode",
                effect=iam.Effect.DENY,
                actions=["s3:GetObject*"],
                not_resources=[code.bucket.arn_for_objects(code.s3_object_key)],
            )
        )

    def _acknowledge_log_retention_provider(self, stack: Stack) -> None:
        """CDK's LogRetention provider is a stack-level singleton (``LogRetention<uuid>``) without an accessor."""
        provider = next(child for child in stack.node.children if child.node.id.startswith("LogRetention"))
        acknowledge_references(
            provider,
            {
                nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "CDK LogRetention provider: logs of "
                "its own function",
                nag.wildcard_resource(): "CDK LogRetention provider: one singleton sets retention policies for every "
                "log group of the stack",
                nag.wildcard_resource(
                    f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group:"
                    f"/aws/bedrock-agentcore/runtimes/{self.runtime.agent_runtime_id}-DEFAULT:*"
                ): "CDK LogRetention provider: deletes the runtime's log group with the stack",
            },
        )

    @property
    def invoke_url(self) -> str:
        """HTTPS invocation endpoint of the DEFAULT endpoint (CONTRACTS §H ``runtimeInvokeUrl``):
        ``https://bedrock-agentcore.<region>.amazonaws.com/runtimes/<url-encoded ARN>/invocations?qualifier=DEFAULT``.

        The ARN is assembled from its parts, so URL-encoding it needs no CloudFormation string functions.
        """
        stack = Stack.of(self)
        encoded_arn = "%3A".join(
            ["arn", stack.partition, "bedrock-agentcore", stack.region, stack.account, "runtime%2F"]
        )
        return (
            f"https://bedrock-agentcore.{stack.region}.{stack.url_suffix}/runtimes/"
            f"{encoded_arn}{self.runtime.agent_runtime_id}/invocations?qualifier=DEFAULT"
        )


def _registry_environment(prefix: str, task: RegistryTask) -> dict[str, str]:
    """The agent's model-registry settings (``fast_channels.agent.settings``)."""
    environment = {
        "MODEL_TASK_PARAMETER": task_parameter(prefix, task.name),
        "MODEL_PROFILE_ARN": task.profile.arn,
        "MODEL_COUNTER_TABLE": counter_table_name(prefix),
    }
    if task.daily_cap_usd is not None:
        environment["MODEL_DAILY_CAP_USD"] = str(task.daily_cap_usd)
    if task.alert_topic_arn is not None:
        environment["ALERT_TOPIC_ARN"] = task.alert_topic_arn
    return environment


def model_invoke_statements(
    models: Sequence[str], *, partition: str, region: str, account: str
) -> list[iam.PolicyStatement]:
    """Least-privilege invoke statements for Bedrock models and system inference profiles (models-ui.md §1.3).

    - ``global.<model>``: the profile in this region, the regional foundation model and the region-less global
      foundation model (``aws:RequestedRegion`` ``unspecified``), both only through that profile.
    - ``<geo>.<model>`` (``us.``, ``eu.``, ...): the profile, plus the foundation model in any region of the
      geography, only through that profile.
    - anything else: a foundation model in this region.
    """
    bedrock = f"arn:{partition}:bedrock"
    statements = []
    for model in models:
        prefix, _, model_id = model.partition(".")
        if prefix not in _PROFILE_PREFIXES:
            statements.append(_invoke([f"{bedrock}:{region}::foundation-model/{model}"]))
            continue
        profile = f"{bedrock}:{region}:{account}:inference-profile/{model}"
        through_profile = {"bedrock:InferenceProfileArn": profile}
        statements.append(_invoke([profile]))
        if prefix == "global":
            statements += [
                _invoke([f"{bedrock}:{region}::foundation-model/{model_id}"], through_profile),
                _invoke(
                    [f"{bedrock}:::foundation-model/{model_id}"],
                    {"aws:RequestedRegion": "unspecified", **through_profile},
                ),
            ]
        else:
            statements.append(_invoke([f"{bedrock}:*::foundation-model/{model_id}"], through_profile))
    return statements


def _invoke(resources: list[str], conditions: dict[str, str] | None = None) -> iam.PolicyStatement:
    return iam.PolicyStatement(
        actions=list(INVOKE_ACTIONS),
        resources=resources,
        conditions={"StringEquals": conditions} if conditions else None,
    )


def _role_wildcards(stack: Stack, models: Sequence[str], runtime_name: str, traces: bool) -> dict[str, str]:
    """cdk-nag findings of the role's unavoidable wildcards, with their reasons (ids may hold tokens)."""
    logs = f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group"
    identities = (
        f"arn:{stack.partition}:bedrock-agentcore:{stack.region}:{stack.account}:workload-identity-directory/"
        "default/workload-identity/*"
    )
    findings = {
        nag.wildcard_resource(): "Runtime L2 role: X-Ray segments and CloudWatch metrics (namespace-scoped) only "
        "support '*'",
        nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/*"): "Runtime L2 role: the service names "
        "the runtime's log groups after its generated id",
        nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/*:log-stream:*"): "Runtime L2 role: log "
        "streams of the runtime's own log groups",
        nag.wildcard_resource(f"{logs}:*"): "Runtime L2 role: logs:DescribeLogGroups has no resource-level permissions",
        nag.wildcard_resource(identities): "Runtime L2 role: access tokens of the runtime's workload identity",
        nag.wildcard_resource(file_asset_objects(stack)): "Runtime L2 role: reads its code zip from the CDK "
        "file-asset bucket",
        **{
            nag.wildcard_action(action): "Runtime L2 role: read grant on the code zip in the CDK asset bucket"
            for action in ("s3:GetObject*", "s3:GetBucket*", "s3:List*")
        },
    }
    if traces:
        findings[nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/{runtime_name}-*")] = (
            "Unified span destination: the runtime's own log groups, named after its generated id"
        )
    for model in models:
        prefix, _, model_id = model.partition(".")
        if prefix in _PROFILE_PREFIXES - {"global"}:
            findings[nag.wildcard_resource(f"arn:{stack.partition}:bedrock:*::foundation-model/{model_id}")] = (
                f"{prefix}. inference profile: routes to any region of its geography; allowed only through that "
                "profile (bedrock:InferenceProfileArn)"
            )
    return findings


def _evaluation_wildcards(stack: Stack, runtime: agentcore.Runtime) -> dict[str, str]:
    """cdk-nag findings of the OnlineEvaluationConfig L2 role, with their reasons."""
    logs = f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group"
    judge = "OnlineEvaluationConfig L2 role: built-in evaluators choose their own judge models"
    return {
        nag.wildcard_resource(): "OnlineEvaluationConfig L2 role: actions without resource-level permissions",
        nag.wildcard_resource("arn:<AWS::Partition>:bedrock:*::foundation-model/*"): judge,
        nag.wildcard_resource(f"arn:{stack.partition}:bedrock:*:{stack.account}:inference-profile/*"): judge,
        nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/evaluations/*"): "OnlineEvaluationConfig L2 role: "
        "evaluation results",
        nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/{runtime.agent_runtime_id}-DEFAULT:*"): (
            "OnlineEvaluationConfig L2 role: reads the evaluated runtime's sessions"
        ),
        nag.wildcard_resource(f"{logs}:aws/spans:*"): "OnlineEvaluationConfig L2 role: reads the spans it scores",
    }
