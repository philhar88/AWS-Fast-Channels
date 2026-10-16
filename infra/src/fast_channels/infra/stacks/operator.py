"""``FastChannels-Operator``: the operator plane (SPEC §6), deployed when ``agent.enabled``.

Skeleton: the constructs of CONTRACTS §F are added at the integration points below, in this order.
"""

from typing import TYPE_CHECKING, Any

from aws_cdk import Stack

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.stacks.data import DataExports
    from fast_channels.infra.stacks.media import MediaStack


class OperatorStack(Stack):
    """Cognito identity, agent tool Lambdas, AgentCore Gateway with its Cedar policy engine, the scheduler agent
    Runtime, autopilot schedules and the operator console.

    Args:
        config: deployment configuration (``agent``, ``channels``, ``observability.transaction_search``).
        data: DataStack references (``library_table``, ``catalog_table``, ``media_bucket``, ``memory``).
        media: the media stack, for the pipeline, origin, channels and distribution its constructs expose.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaStack,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): operator plane", **kwargs)
        # Remove once the first construct below exists: CloudFormation cannot deploy a stack without resources.
        nag.acknowledge(self, {nag.EMPTY_TEMPLATE: "Skeleton: its constructs are added at the integration points"})
        # Integration points (stage-2 operator agents), every Lambda via constructs.functions.FastFunction:
        #
        # 1. operator/identity.OperatorIdentity(admin_email=config.agent.admin_email, callback_urls=[console URL]).
        # 2. operator/tools.AgentTools(library=data.library_table, catalog=data.catalog_table,
        #    media_bucket=data.media_bucket, + media's pipeline, origin and channels): one Lambda per target.
        # 3. operator/gateway.AgentGateway(tools, identity): Cedar from the tool registry, ENFORCE mode. Gateway and
        #    Memory (data.memory) log deliveries; TRACES only when config.observability.transaction_search != "off"
        #    (with "enable" the stage makes this stack depend on AccountBaselineStack).
        # 4. operator/runtime.SchedulerAgent(gateway, identity, memory=data.memory, model=config.agent.model,
        #    allowed_models=config.agent.allowed_models).
        # 5. operator/autopilot.Autopilot for each config.channels.iac entry with an autopilot_cron.
        # 6. operator/console.OperatorConsole(identity, agent, distribution=media.distribution).
