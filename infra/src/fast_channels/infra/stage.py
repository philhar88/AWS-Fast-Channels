"""``FastChannelsStage``: one FAST Channels deployment, as a set of stacks."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Aspects, Environment, Stage, Tags

from fast_channels.core.demand import NETWORK_CONTEXT, DemandNetwork
from fast_channels.core.tags import Owner, resource_tags
from fast_channels.infra.config import ConfigError, FastChannelsConfig, Removal, TransactionSearchMode
from fast_channels.infra.constructs.functions import (
    FunctionLogGroups,
    LambdaDefaults,
    LogGroupDefaults,
    StateMachineDefaults,
)
from fast_channels.infra.constructs.release.guards import ReleaseGuard
from fast_channels.infra.stacks.as_run import AsRunRouterStack
from fast_channels.infra.stacks.baseline import AccountBaselineStack
from fast_channels.infra.stacks.crawl import CrawlStack
from fast_channels.infra.stacks.data import DataStack
from fast_channels.infra.stacks.demand import DemandStack
from fast_channels.infra.stacks.demand_build import DemandBuildStack
from fast_channels.infra.stacks.demand_edge import DemandEdgeStack
from fast_channels.infra.stacks.edge import EDGE_REGION, EdgeStack
from fast_channels.infra.stacks.media import MediaStack
from fast_channels.infra.stacks.models import ModelsStack
from fast_channels.infra.stacks.operator import OperatorStack
from fast_channels.infra.stacks.ops import OpsStack
from fast_channels.infra.stacks.pds import PdsStack
from fast_channels.infra.stacks.release import ReleaseStack

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
    us-east-1, when ``env.region`` is another region; named ``<stage>-Edge-<region>``), AccountBaseline (the
    account's Transaction Search, only with ``account_baseline``), DemandEdge (in us-east-1) and DemandBuild (if
    ``modules.demand.enabled``; the Prebid Server guidance itself deploys as its own app, docs/demand.md), Demand
    beside the guidance once ``scripts/prebid_guidance.py`` passes its network (context
    ``fast-channels:demand-network``), Pds after it (if ``modules.pds.enabled``), and AsRun (the account's as-run
    router, only with ``as_run_router``).

    Cross-stack references are weak (``Fn::GetStackOutput``, cdk.json), so stateless stacks can be refactored
    without export locks. Every Lambda function and state machine in the stage gets the
    :mod:`~fast_channels.infra.constructs.functions` runtime defaults; every resource is tagged ``fast:app`` and
    ``fast:owner``. With ``removal: retain`` the stacks are termination protected.

    Args:
        config: validated deployment configuration; it is validated again for ``env.region``
            (:meth:`~fast_channels.infra.config.FastChannelsConfig.for_region`), which resolves ``{geo}`` model ids.
        env: account (may be unresolved) and region (required) of the deployment.
        as_run_router: add the account's as-run router stack ``FastChannels-AsRun`` (``make as-run-router``, ADR-0032).
            It is never part of the stage otherwise, so ``make deploy`` and ``make destroy`` leave it alone.
        account_baseline: add the account baseline stack ``FastChannels-AccountBaseline`` (``make account-baseline``),
            which needs ``observability.transaction_search: enable``. It is never part of the stage otherwise, so
            ``make deploy`` and ``make destroy`` leave it, and its termination protection, alone.

    Raises:
        ConfigError: ``account_baseline`` is set while ``observability.transaction_search`` is not ``enable``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        env: Environment,
        as_run_router: bool = False,
        account_baseline: bool = False,
    ) -> None:
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

        self.as_run = self._as_run_router(config, tags) if as_run_router else None

        self.baseline = self._account_baseline(config, tags) if account_baseline else None

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

        self._modules(config, env, stacks)

        Aspects.of(self).add(FunctionLogGroups(log_groups))
        for key, value in tags.items():
            Tags.of(self).add(key, value)

    def _modules(self, config: FastChannelsConfig, env: Environment, stacks: dict[str, Any]) -> None:
        """The demand module's stacks (and Pds and Release beside the guidance), and the release module's guard."""
        self.demand_edge: DemandEdgeStack | None = None
        self.demand_build: DemandBuildStack | None = None
        self.demand: DemandStack | None = None
        self.pds: PdsStack | None = None
        self.release: ReleaseStack | None = None
        if config.modules.demand.enabled:
            self._demand_stacks(config, env, stacks)
        if config.modules.release.enabled:
            Aspects.of(self).add(self._release_guard(config, str(env.region)))

    def _demand_stacks(self, config: FastChannelsConfig, env: Environment, stacks: dict[str, Any]) -> None:
        """DemandEdge (in us-east-1), DemandBuild, and Demand (then Pds) once the deploy script passes the guidance's
        network."""
        self.demand_edge = DemandEdgeStack(
            self,
            "DemandEdge",
            stack_name=f"{self.stage_name}-DemandEdge" + ("" if env.region == EDGE_REGION else f"-{env.region}"),
            config=config,
            env=Environment(account=env.account, region=EDGE_REGION),
            **stacks,
        )
        self.demand_build = DemandBuildStack(self, "DemandBuild", config=config, **stacks)
        if (raw := self.node.try_get_context(NETWORK_CONTEXT)) is None:
            return
        network = DemandNetwork.model_validate_json(raw) if isinstance(raw, str) else DemandNetwork.model_validate(raw)
        self.demand = DemandStack(self, "Demand", config=config, network=network, **stacks)
        if config.modules.pds.enabled:
            self.pds = PdsStack(self, "Pds", config=config, network=network, zone=self.demand.dns.zone, **stacks)
            self.pds.add_stack_dependency(self.data, "The PDS's table and config bucket are the Data stack's")
            self.pds.add_stack_dependency(self.demand, "The PDS's record is in the Demand stack's private zone")
            if self.ops is not None:
                self.pds.add_stack_dependency(self.ops, "The decision records' tables are in the lake's database")
        if config.modules.release.enabled:
            self.release = ReleaseStack(self, "Release", config=config, network=network, **stacks)
            for stack, why in (
                (self.data, "The ledger is the Data stack's"),
                (self.media, "The channels and the SSM parameters the bundle reads"),
                (self.ops, "The lake's delivery destinations"),
                (self.demand, "Deployed beside the guidance"),
                (self.pds, "Siblings POST to Prebid Server, which asks the PDS"),
            ):
                if stack is not None:
                    self.release.add_stack_dependency(stack, why)

    def _release_guard(self, config: FastChannelsConfig, region: str) -> ReleaseGuard:
        """Only the Release Controller (its apply path and reaper) may change monetization (DESIGN §8.1)."""
        exempt = []
        if self.release is not None:
            controller = self.release.controller
            # The controller's writers, and the readers of the siblings' logs (the zero-ad-sessions detector).
            exempt = [controller.apply.node.path, controller.reaper.node.path, self.release.zero_ads.function.node.path]
        return ReleaseGuard(
            prefix=config.prefix, channels=config.modules.release.channels, region=region, exempt=exempt
        )

    def _account_baseline(self, config: FastChannelsConfig, tags: dict[str, str]) -> AccountBaselineStack:
        """The account's Transaction Search baseline, which only ``transaction_search: enable`` asks for."""
        if (mode := config.observability.transaction_search) is not TransactionSearchMode.ENABLE:
            raise ConfigError(
                "the account baseline turns Transaction Search on for the account; "
                f"observability.transaction_search is {mode.value!r}: set it to 'enable'"
            )
        return AccountBaselineStack(self, "AccountBaseline", tags=tags)

    def _as_run_router(self, config: FastChannelsConfig, tags: dict[str, str]) -> AsRunRouterStack:
        """The account's as-run router stack, after Media (whose custom resource creates the as-run log group)."""
        stack = AsRunRouterStack(
            self,
            "AsRun",
            log_retention=config.observability.log_retention,
            notification_email=config.notifications.email,
            tags=tags,
        )
        stack.add_stack_dependency(self.media, "The as-run log group exists")
        return stack
