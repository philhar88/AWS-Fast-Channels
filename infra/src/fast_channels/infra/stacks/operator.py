"""``FastChannels-Operator``: the operator plane (SPEC §6), deployed when ``agent.enabled``."""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack

from fast_channels.core.model_registry import SCHEDULER_TASK
from fast_channels.core.naming import agent_namespace, agentcore_name
from fast_channels.functions.tools.registry import confirm_tools
from fast_channels.infra.config import TransactionSearchMode
from fast_channels.infra.constructs.alerts import alert_topic_arn
from fast_channels.infra.constructs.models.caps import TaskBudget
from fast_channels.infra.constructs.models.profiles import profile_name
from fast_channels.infra.constructs.operator.autopilot import Autopilot
from fast_channels.infra.constructs.operator.console import CONSOLE_DIST, ConsoleConfig, OperatorConsole
from fast_channels.infra.constructs.operator.deliveries import AgentTelemetry
from fast_channels.infra.constructs.operator.gateway import AgentGateway
from fast_channels.infra.constructs.operator.identity import OperatorIdentity, managed_login_url
from fast_channels.infra.constructs.operator.runtime import RegistryTask, SchedulerAgent
from fast_channels.infra.constructs.operator.tools import AgentTools

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports, ModelsExports


class OperatorStack(Stack):
    """Cognito identity, agent tool Lambdas, the AgentCore Gateway with its Cedar policy engine, the scheduler agent
    Runtime, autopilot schedules and the operator console.

    The autopilot (its machine client, Cedar permits, schedules, function and alarms) exists only when some IaC
    channel has an ``autopilot_cron``: its client secret is readable by anyone allowed
    ``cognito-idp:DescribeUserPoolClient`` (see :class:`OperatorIdentity`).

    The console's distribution and the Cognito clients live in this one stack: the clients redirect to the
    console URL and the console's ``config.json`` names the clients, which would be a cycle across stacks.

    Args:
        config: deployment configuration (``agent``, ``channels``, ``channel_assembly``, ``observability``,
            ``modules.models``).
        data: Data stack references (tables, buckets, Memory); ``data.memory`` must exist.
        media: Media stack exports (the only thing the operator plane reads from the media plane).
        models: Models stack exports (the agent's task profile) when the model registry is deployed.
        console_dist: the console build (``console/dist``).

    Attributes:
        identity, tools, gateway, agent, console: the operator constructs; autopilot is ``None`` when no IaC
        channel has an ``autopilot_cron``; budget is the scheduler task's AWS Budgets action with
        ``modules.models.budgets_action`` and a daily cap, else ``None``.

    Raises:
        ValueError: ``data.memory`` is missing.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaExports,
        models: ModelsExports | None = None,
        console_dist: Path = CONSOLE_DIST,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): operator plane", **kwargs)
        if data.memory is None:
            raise ValueError("the operator plane needs the agent's AgentCore Memory (agent.enabled)")
        prefix = config.prefix
        retention, removal = config.observability.log_retention, config.removal_policy
        traces = config.observability.transaction_search is not TransactionSearchMode.OFF
        domain_prefix = f"{prefix}-{self.account}"
        crons = {channel.name: channel.autopilot_cron for channel in config.channels.iac if channel.autopilot_cron}

        self.console = OperatorConsole(
            self,
            "Console",
            cognito_domain_url=managed_login_url(domain_prefix, self.region),
            media_urls=[media.cdn_base_url, media.content_base_url],
            dist=console_dist,
        )
        self.identity = OperatorIdentity(
            self,
            "Identity",
            domain_prefix=domain_prefix,
            callback_urls=[self.console.url],
            console_dev_url=config.agent.console_dev_url,
            autopilot=bool(crons) or config.agent.machine_client,
            admin_email=config.agent.admin_email,
            removal_policy=removal,
        )
        self.tools = AgentTools(
            self,
            "Tools",
            prefix=prefix,
            data=data,
            media=media,
            package_profiles=config.channel_assembly.package_profiles,
            auto_stop_hours=config.channels.max_runtime_hours,
            max_agent_channels=config.channels.max_agent_channels,
            cdns=config.delivery.cdns,
            log_retention=retention,
            removal_policy=removal,
            epg_break_ids=config.modules.ops.enabled,
        )
        self.gateway = AgentGateway(
            self,
            "Gateway",
            name=f"{prefix}-tools",
            policy_engine_name=agentcore_name(prefix, "tools"),
            identity=self.identity,
            targets=self.tools.targets,
            agent_channel_prefix=agent_namespace(prefix),
            autopilot_channels=tuple(crons),
        )
        model_task = None
        if models is not None:
            model_task = RegistryTask(
                name=SCHEDULER_TASK,
                profile=models.profiles[SCHEDULER_TASK],
                settings=config.modules.models.tasks[SCHEDULER_TASK],
                alert_topic_arn=alert_topic_arn(self, prefix),
            )
        model = model_task.profile.model if model_task else config.agent.model
        self.agent = SchedulerAgent(
            self,
            "Agent",
            runtime_name=agentcore_name(prefix, "scheduler"),
            identity=self.identity,
            gateway_url=self.gateway.url,
            memory=data.memory,
            memory_preference_namespace=data.memory_preference_namespace,
            model=model,
            allowed_models=(model,) if model_task else config.agent.allowed_models,
            confirm_tools=confirm_tools(),
            prefix=prefix,
            traces=traces,
            log_retention=retention,
            removal_policy=removal,
            record_prompts=config.observability.records_prompts,
            model_task=model_task,
        )
        self.budget: TaskBudget | None = None
        email = config.notifications.email
        cap = model_task.settings.daily_cap_usd if model_task else None
        if model_task and cap and config.modules.models.budgets_action and email:
            self.budget = TaskBudget(
                self,
                "Budget",
                budget_name=profile_name(prefix, SCHEDULER_TASK),
                task=SCHEDULER_TASK,
                daily_cap_usd=cap,
                role=self.agent.runtime.role,
                profile_arn=model_task.profile.arn,
                email=email,
            )
        AgentTelemetry(
            self,
            "Telemetry",
            prefix=prefix,
            resources={
                "gateway": self.gateway.gateway.gateway_arn,
                "memory": data.memory.memory_arn,
                "runtime": self.agent.runtime.agent_runtime_arn,
            },
            traces=traces,
            log_retention=retention,
            removal_policy=removal,
        )
        self.autopilot: Autopilot | None = None
        if crons:
            self.autopilot = Autopilot(
                self,
                "Autopilot",
                channels=crons,
                identity=self.identity,
                runtime_invoke_url=self.agent.invoke_url,
                event_bus=media.event_bus,
                prefix=prefix,
                log_retention=retention,
                removal_policy=removal,
                alert_topic=media.alert_topic,
            )
        AgentGateway.protect(
            self.agent.runtime,
            *(function.function for function in self.tools.functions.values()),
            *([self.autopilot.function.function] if self.autopilot else []),
        )
        self.console.publish(
            ConsoleConfig(
                region=self.region,
                user_pool_id=self.identity.user_pool.user_pool_id,
                user_pool_client_id=self.identity.console_client.user_pool_client_id,
                cognito_domain=self.identity.domain_url,
                runtime_arn=self.agent.runtime.agent_runtime_arn,
                runtime_invoke_url=self.agent.invoke_url,
                gateway_url=self.gateway.url,
                cdn_base_url=media.cdn_base_url,
                channels=media.channels,
            )
        )
        self._outputs()

    def _outputs(self) -> None:
        """Values for operators and scripts (e2e, eval_model.py --gateway-url); ``AutopilotClientId`` only when the
        machine client exists (an autopilot, or ``agent.machine_client``), ``ModelSpecParameter`` only without the
        model registry."""
        outputs = {
            "ConsoleUrl": self.console.url,
            "UserPoolId": self.identity.user_pool.user_pool_id,
            "ConsoleClientId": self.identity.console_client.user_pool_client_id,
            "CognitoDomain": self.identity.domain_url,
            "GatewayUrl": self.gateway.url,
            "RuntimeArn": self.agent.runtime.agent_runtime_arn,
            "RuntimeInvokeUrl": self.agent.invoke_url,
            **({"ModelSpecParameter": self.agent.model_spec.parameter_name} if self.agent.model_spec else {}),
            "ToolFunctions": self.to_json_string(
                {target: function.function.function_name for target, function in self.tools.functions.items()}
            ),
        }
        if self.identity.autopilot_client is not None:
            outputs["AutopilotClientId"] = self.identity.autopilot_client.user_pool_client_id
        for name, value in outputs.items():
            CfnOutput(self, name, value=value)
