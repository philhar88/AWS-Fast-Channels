"""IaC linear channels: a Channel Assembly channel with its own ad insertion, SigV4-only channel policy and
lifecycle custom resources (SPEC §5.3)."""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Self

from aws_cdk import Aws, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediatailor as mediatailor
from constructs import Construct

from fast_channels.core import specs
from fast_channels.core.profiles import profile
from fast_channels.infra.arns import arn
from fast_channels.infra.config import ChannelTier
from fast_channels.infra.constructs._provider import provider
from fast_channels.infra.constructs.channel_assembly._cfn import override, with_tokens
from fast_channels.infra.constructs.channel_assembly.ad_insertion import AdInsertion, MediaTailorLogs
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.exports import ChannelExports

if TYPE_CHECKING:
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import custom_resources as cr

    from fast_channels.core.timeline import ProgrammingBrief
    from fast_channels.infra.constructs.channel_assembly.origin import ChannelAssemblyOrigin

_CHANNEL_HANDLER: Final = "fast_channels.functions.custom_resources.channel_lifecycle.handler"


class _ChannelProvider(Construct):
    """The stack's custom-resource provider for every :class:`LinearChannel` (one Lambda, three resource types)."""

    def __init__(self, scope: Construct, id: str, *, log_retention: logs.RetentionDays, removal_policy: RemovalPolicy):
        super().__init__(scope, id)
        self.handler = FastFunction(
            self,
            "Handler",
            handler=_CHANNEL_HANDLER,
            description="Linear channel custom resources: update guard, endpoint discovery, lifecycle",
            timeout=Duration.minutes(10),  # draining 400 programs at 3 TPS takes about 2.5 minutes
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        # The update guard stands down while its stack rolls back.
        self.handler.allow(["cloudformation:DescribeStacks"], [Aws.STACK_ID])
        self.provider: cr.Provider = provider(
            self, "Provider", on_event=self.handler, log_retention=log_retention, removal_policy=removal_policy
        )

    @classmethod
    def of(cls, scope: Construct, *, log_retention: logs.RetentionDays, removal_policy: RemovalPolicy) -> Self:
        stack = Stack.of(scope)
        existing = stack.node.try_find_child("LinearChannelProvider")
        if isinstance(existing, cls):
            return existing
        return cls(stack, "LinearChannelProvider", log_retention=log_retention, removal_policy=removal_policy)


class LinearChannel(Construct):
    """A Channel Assembly ``LINEAR`` channel owned by CloudFormation, with its per-channel ad insertion.

    - ``CfnChannel``: the filler slate, one output per rendition profile (``core.specs.channel_outputs``: HLS with
      DATERANGE ad markers, which SGAI requires) and AS_RUN logging. Channels are created stopped and are never
      started automatically.
    - :class:`AdInsertion` ``<name>``, and a channel policy (``core.specs.channel_policy``) that lets only that
      playback configuration read the channel, with SigV4; anonymous requests get 403 (channel-assembly.md §6.1).
    - Three custom resources (``fast_channels.functions.custom_resources.channel_lifecycle``): a guard that fails an
      update of a running channel fast (or stops it when ``restart_on_update``), the endpoint lookup (CloudFormation
      exposes only the ARN), and the lifecycle that writes the ``CHANNEL#<name>`` ``META`` item, restarts a channel
      the guard stopped, and on deletion stops the channel and deletes its programs first. If an update fails after
      the guard stopped the channel, the rollback leaves it stopped.

    The filler slate cannot be deleted while a channel uses it ("There is at least one channel that uses it for
    filler slate", observed live), so register channels with :meth:`RuntimeResourceReaper.run_after_deleting`, and
    make them depend on whatever provisions the filler slate.

    Args:
        name: channel name (``<prefix>-...``, never ``<prefix>-agent-...``); also the playback configuration name.
        ladder_version: ladder of every program, slate and ad on the channel.
        outputs: rendition profiles published as outputs; every VOD source must be packaged for each.
        filler_slate: VOD source (in ``origin``) of the ladder's filler slate; it must exist before the channel.
        origin: the deployment's source location.
        vended_logs: vended-logs destination for the playback configuration.
        library: media library table (``CHANNEL#`` items).
        content_base_url: base URL of content and ad segments.
        log_retention: retention of the custom-resource function logs.
        ads_url: external ADS URL; default ``core.specs.ads_url`` (the ladder's preconditioned VAST pod).
        tier: channel tier (BASIC or STANDARD).
        brief: initial programming brief; the agent may change it later (deployments only overwrite it when this
            value changes).
        log_percent: share of playback sessions that emit vended logs.
        restart_on_update: let deployments stop a running channel to update it, and start it again.
        removal_policy: what happens to the custom-resource function logs when the stack is deleted.

    Attributes:
        channel: the ``CfnChannel``. ad_insertion: its :class:`AdInsertion`. exports: :class:`ChannelExports`.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        ladder_version: str,
        outputs: Sequence[str],
        filler_slate: str,
        origin: ChannelAssemblyOrigin,
        vended_logs: MediaTailorLogs,
        library: dynamodb.ITableV2,
        content_base_url: str,
        log_retention: logs.RetentionDays,
        ads_url: str | None = None,
        tier: ChannelTier = ChannelTier.BASIC,
        brief: ProgrammingBrief | None = None,
        log_percent: int = 100,
        restart_on_update: bool = False,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.name, self.ladder_version, self.outputs = name, ladder_version, tuple(outputs)
        shared = _ChannelProvider.of(self, log_retention=log_retention, removal_policy=removal_policy)
        spec = {
            "PlaybackMode": "LINEAR",
            "Tier": str(tier),
            "FillerSlate": {"SourceLocationName": origin.name, "VodSourceName": filler_slate},
            "Outputs": specs.channel_outputs([profile(o) for o in self.outputs]),
            "LogConfiguration": {"LogTypes": ["AS_RUN"]},
        }

        def resource(id: str, resource_type: str, **properties: Any) -> CustomResource:
            return CustomResource(
                self,
                id,
                service_token=shared.provider.service_token,
                resource_type=resource_type,
                properties={"ChannelName": name, **properties},
            )

        guard = resource("Guard", "Custom::ChannelGuard", RestartOnUpdate=restart_on_update, Spec=spec)
        self.channel = mediatailor.CfnChannel(self, "Resource", channel_name=name, playback_mode="LINEAR", outputs=[])
        override(self.channel, {key: value for key, value in spec.items() if key != "PlaybackMode"})
        self.channel.node.add_dependency(guard, origin.source_location)

        endpoint = resource("Endpoint", "Custom::ChannelEndpoint", Outputs=list(self.outputs))
        endpoint.node.add_dependency(self.channel)
        channel_url = endpoint.get_att_string("ChannelUrl")
        preconditioned = {"content_base_url": content_base_url, "ladder_version": ladder_version, "channel": name}
        self.ad_insertion = AdInsertion(
            self,
            "AdInsertion",
            name=name,
            channel_url=channel_url,
            ads_url=ads_url or with_tokens(specs.ads_url, **preconditioned),
            content_base_url=content_base_url,
            vended_logs=vended_logs,
            log_percent=log_percent,
        )
        policy = mediatailor.CfnChannelPolicy(
            self,
            "Policy",
            channel_name=name,
            policy=specs.channel_policy(
                channel_arn=self.channel.attr_arn,
                playback_configuration_arn=self.ad_insertion.arn,
                account=Aws.ACCOUNT_ID,
            ),
        )
        policy.add_resource_dependency(self.channel)

        lifecycle = resource(
            "Lifecycle",
            "Custom::ChannelLifecycle",
            TableName=library.table_name,
            RestartToken=guard.get_att_string("StoppedForUpdate"),
            Brief=json.dumps(brief.model_dump(mode="json"), sort_keys=True) if brief else "",
            Record={
                "name": name,
                "arn": self.channel.attr_arn,
                "tier": str(tier),
                "ladder_version": ladder_version,
                "outputs": list(self.outputs),
                "playback_configuration_name": self.ad_insertion.name,
                "playback_configuration_arn": self.ad_insertion.arn,
                "hls_prefix": self.ad_insertion.hls_prefix,
                "channel_url": channel_url,
                "source_location": origin.name,
                "filler_slate": filler_slate,
            },
        )
        lifecycle.node.add_dependency(policy)
        self._grant(shared.handler, library)

    def _grant(self, handler: FastFunction, library: dynamodb.ITableV2) -> None:
        handler.allow(
            [
                "mediatailor:DescribeChannel",
                "mediatailor:StartChannel",
                "mediatailor:StopChannel",
                "mediatailor:GetChannelSchedule",
            ],
            [arn(self, "mediatailor", "channel", self.name)],
        )
        handler.allow(
            ["mediatailor:DeleteProgram"],
            [arn(self, "mediatailor", "program", f"{self.name}/*")],
            why="Programs are created at runtime by the schedule tools",
        )
        handler.allow(
            ["dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"],
            [library.table_arn],
            conditions={"ForAllValues:StringEquals": {"dynamodb:LeadingKeys": [f"CHANNEL#{self.name}"]}},
        )

    @property
    def exports(self) -> ChannelExports:
        """What the operator plane needs about this channel (``MediaExports.channels``)."""
        return ChannelExports(
            name=self.name,
            arn=self.channel.attr_arn,
            ladder_version=self.ladder_version,
            outputs=self.outputs,
            playback_configuration_name=self.ad_insertion.name,
            hls_prefix=self.ad_insertion.hls_prefix,
        )
