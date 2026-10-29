"""``FastChannelsStage``: one FAST Channels deployment, as a set of stacks."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Aspects, Environment, Stage, Tags

from fast_channels.core.tags import Owner, resource_tags
from fast_channels.infra.config import FastChannelsConfig, Removal, TransactionSearchMode
from fast_channels.infra.constructs.functions import (
    FunctionLogGroups,
    LambdaDefaults,
    LogGroupDefaults,
    StateMachineDefaults,
)
from fast_channels.infra.stacks.baseline import AccountBaselineStack
from fast_channels.infra.stacks.crawl import CrawlStack
from fast_channels.infra.stacks.data import DataStack
from fast_channels.infra.stacks.edge import EDGE_REGION, EdgeStack
from fast_channels.infra.stacks.media import MediaStack
from fast_channels.infra.stacks.models import ModelsStack
from fast_channels.infra.stacks.operator import OperatorStack
from fast_channels.infra.stacks.ops import OpsStack

if TYPE_CHECKING:
    from constructs import Construct

DEFAULT_PREFIX: Final = "fast"
OWNER_TAG: Final = Owner.IAC.value
"""``fast:owner`` of CloudFormation-managed resources (``core.tags.Owner.IAC``); runtime resources carry
``pipeline`` or ``agent``."""


def stage_id(prefix: str) -> str:
    """Stage id, which prefixes every stack name: ``FastChannels`` (``FastChannels-Data``, ...) for the default
    prefix, ``FastChannels-<prefix>`` for further deployments in the same account and region."""
    return "FastChannels" if prefix == DEFAULT_PREFIX else f"FastChannels-{prefix}"


class FastChannelsStage(Stage):
    """Data → Media → Models (if ``modules.models.enabled``) → Operator (if ``agent.enabled``), plus Crawl (if
    ``modules.crawl.enabled``, after Media), Ops (if ``modules.ops.enabled``, after Data and Media), Edge (in
    us-east-1, when ``env.region`` is another region; named ``<stage>-Edge-<region>``) and AccountBaseline (if
    ``observability.transaction_search`` is ``enable``).

    Cross-stack references are weak (``Fn::GetStackOutput``, cdk.json), so stateless stacks can be refactored
    without export locks. Every Lambda function and state machine in the stage gets the
    :mod:`~fast_channels.infra.constructs.functions` runtime defaults; every resource is tagged ``fast:app`` and
    ``fast:owner``. With ``removal: retain`` the stacks are termination protected.

    Args:
        config: validated deployment configuration; it is validated again for ``env.region``
            (:meth:`~fast_channels.infra.config.FastChannelsConfig.for_region`), which resolves ``{geo}`` model ids.
        env: account (may be unresolved) and region (required) of the deployment.
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, env: Environment) -> None:
        if not env.region:
            raise ValueError("the deployment region must be known at synth time (set AWS_REGION or a profile region)")
        config = config.for_region(env.region)
        log_groups = LogGroupDefaults(config.observability.log_retention, config.removal_policy)
        super().__init__(
            scope,
            id,
            env=env,
            # jsii hands InjectionContext to Python injectors positionally; its stubs show lifted keywords.
            property_injectors=[LambdaDefaults(log_groups), StateMachineDefaults(log_groups)],  # type: ignore[list-item]
        )
        tags = resource_tags(config.prefix, Owner.IAC)
        stacks: dict[str, Any] = {"tags": tags, "termination_protection": config.removal is Removal.RETAIN}

        self.data = DataStack(self, "Data", config=config, **stacks)
        self.media = MediaStack(self, "Media", config=config, data=self.data.exports, **stacks)
        self.media.add_stack_dependency(self.data)

        self.ops: OpsStack | None = None
        if config.modules.ops.enabled:
            self.ops = OpsStack(self, "Ops", config=config, data=self.data.exports, media=self.media.exports, **stacks)
            self.ops.add_stack_dependency(self.data)
            self.ops.add_stack_dependency(self.media, "The lake copies the IaC playback configurations' log sources")

        self.baseline: AccountBaselineStack | None = None
        if config.observability.transaction_search is TransactionSearchMode.ENABLE:
            self.baseline = AccountBaselineStack(self, "AccountBaseline", tags=tags)

        self.models: ModelsStack | None = None
        if config.modules.models.enabled:
            self.models = ModelsStack(self, "Models", config=config, **stacks)

        self.operator: OperatorStack | None = None
        if config.agent.enabled:
            self.operator = OperatorStack(
                self,
                "Operator",
                config=config,
                data=self.data.exports,
                media=self.media.exports,
                models=self.models.exports if self.models else None,
                **stacks,
            )
            self.operator.add_stack_dependency(self.data, "AgentCore Memory is ACTIVE before the agent plane")
            self.operator.add_stack_dependency(self.media)
            if self.models is not None:
                self.operator.add_stack_dependency(self.models, "The agent reads its model registry entry")
            if self.baseline is not None:
                self.operator.add_stack_dependency(self.baseline, "Gateway and Memory traces need Transaction Search")

        self.crawl: CrawlStack | None = None
        if config.modules.crawl.enabled:
            self.crawl = CrawlStack(
                self, "Crawl", config=config, data=self.data.exports, media=self.media.exports, **stacks
            )
            self.crawl.add_stack_dependency(self.data)
            self.crawl.add_stack_dependency(self.media)

        self.edge: EdgeStack | None = None
        if env.region != EDGE_REGION:
            self.edge = EdgeStack(
                self,
                "Edge",
                # One Edge stack per deployment region: the same prefix may be deployed in several regions.
                stack_name=f"{self.stage_name}-Edge-{env.region}",
                config=config,
                data=self.data.exports,
                distribution=self.media.distribution,
                env=Environment(account=env.account, region=EDGE_REGION),
                **stacks,
            )
            self.edge.add_stack_dependency(self.data)
            self.edge.add_stack_dependency(self.media)

        Aspects.of(self).add(FunctionLogGroups(log_groups))
        for key, value in tags.items():
            Tags.of(self).add(key, value)
