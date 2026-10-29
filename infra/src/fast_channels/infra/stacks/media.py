"""``FastChannels-Media``: the media plane (SPEC §5)."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_logs as logs

from fast_channels.core.naming import source_location_name
from fast_channels.infra.config import AdsMode, AsRunMode, DemandMode, PriceClass
from fast_channels.infra.constructs.alerts import CLOUDFRONT_REGION
from fast_channels.infra.constructs.channel_assembly import (
    AS_RUN_LOG_GROUP,
    AsRunLogging,
    ChannelAssemblyOrigin,
    LinearChannel,
    MediaTailorLogs,
    RuntimeResourceReaper,
    playback_configuration_template,
)
from fast_channels.infra.constructs.delivery import CdnAccessLogs, MediaDistribution
from fast_channels.infra.constructs.encoding import EncodingLadder, IngestPipeline, LadderKit, LadderProvisioner
from fast_channels.infra.constructs.events import FastEventBus
from fast_channels.infra.constructs.observability import MediaObservability
from fast_channels.infra.exports import ChannelExports, MediaExports

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import ChannelConfig, FastChannelsConfig
    from fast_channels.infra.exports import DataExports

__all__ = ["ChannelExports", "MediaExports", "MediaStack"]

_PRICE_CLASSES: Final = {
    PriceClass.ALL: cloudfront.PriceClass.PRICE_CLASS_ALL,
    PriceClass.P200: cloudfront.PriceClass.PRICE_CLASS_200,
    PriceClass.P100: cloudfront.PriceClass.PRICE_CLASS_100,
}


class MediaStack(Stack):
    """Ingest pipeline, ladders and their kits, Channel Assembly origin, IaC channels with their playback
    configurations, CloudFront distribution, runtime-resource reaper and media observability.

    Deletion order (CloudFormation deletes a resource before the resources it depends on): the IaC channels and
    the pipeline's Register step (the only producer of VOD sources left once the Operator stack is gone) go first,
    then the :class:`RuntimeResourceReaper` empties what remains, then the source location, the shared vended-logs
    destination and the MediaConvert queue that the reaped resources would otherwise block.

    Args:
        config: deployment configuration (``ladders``, ``channel_assembly``, ``channels``, ``delivery``, ``ads``,
            ``observability``, ``notifications``).
        data: DataStack references; never add resource-policy statements to them (DataStack owns the policies).

    Attributes:
        exports: what the operator plane consumes (:class:`MediaExports`).
        distribution: the media distribution (EdgeStack logs it when the stage is not in us-east-1).
        access_logs: its standard-logs-v2 delivery when this stack is in us-east-1, else ``None``.
        events, delivery, origin, vended_logs, pipeline, ladders, provisioner, kits, reaper, as_run, channels,
        observability: the media-plane constructs (``ladders`` and ``kits`` by built-in ladder name).
    """

    def __init__(
        self, scope: Construct, id: str, *, config: FastChannelsConfig, data: DataExports, **kwargs: Any
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): media plane", **kwargs)
        prefix = config.prefix
        retention, removal = config.observability.log_retention, config.removal_policy
        default_ladder_version = config.ladder(config.ladders.default).version

        self.events = FastEventBus(
            self,
            "Events",
            prefix=prefix,
            log_retention=retention,
            archive_retention=Duration.days(config.observability.log_retention_days),
            removal_policy=removal,
        )
        self.delivery = MediaDistribution(
            self,
            "Delivery",
            media_bucket=data.media_bucket,
            content_domain=config.delivery.content_domain,
            certificate_arn=config.delivery.certificate_arn,
            price_class=_PRICE_CLASSES[config.delivery.price_class],
        )
        self.distribution = self.delivery.distribution
        self.access_logs: CdnAccessLogs | None = None
        if self.region == CLOUDFRONT_REGION:
            self.access_logs = CdnAccessLogs(
                self, "CdnAccessLogs", distribution=self.distribution, logs_bucket=data.logs_bucket, name=prefix
            )
        content_base_url = self.delivery.content_base_url

        # The Data bucket policy admits exactly this source location (by the same name).
        self.origin = ChannelAssemblyOrigin(
            self,
            "Origin",
            name=source_location_name(prefix),
            bucket=data.media_bucket,
            content_base_url=content_base_url,
        )
        self.vended_logs = MediaTailorLogs(
            self, "MediaTailorLogs", prefix=prefix, log_retention=retention, removal_policy=removal
        )
        self.pipeline = IngestPipeline(
            self,
            "Ingest",
            prefix=prefix,
            ingest_bucket=data.ingest_bucket,
            media_bucket=data.media_bucket,
            library=data.library_table,
            event_bus=self.events.bus,
            source_location_name=self.origin.name,
            content_base_url=content_base_url,
            package_profiles=config.channel_assembly.package_profiles,
            default_ladder_version=default_ladder_version,
            log_retention=retention,
            removal_policy=removal,
        )
        self.ladders = {
            name: EncodingLadder(
                self,
                f"Ladder{name.title()}",
                ladder=config.ladder(name),
                prefix=prefix,
                queue_arn=self.pipeline.queue_arn,
            )
            for name in config.ladders.builtin
        }
        demand = config.modules.demand
        # Demand demo mode: the kits also encode the demo creatives the bidder simulator and direct lines serve.
        demo = demand.enabled and demand.mode == DemandMode.DEMO
        self.provisioner = LadderProvisioner(self, "Kits", pipeline=self.pipeline, demo=demo)
        self.kits = {
            name: LadderKit(self, f"Kit{name.title()}", ladder=self.ladders[name], provisioner=self.provisioner)
            for name in _kit_ladders(config)
        }
        for kit in self.kits.values():
            kit.node.add_dependency(self.origin)  # kit VOD sources live in the source location

        self.reaper = RuntimeResourceReaper(
            self,
            "Reaper",
            prefix=prefix,
            origin=self.origin,
            library=data.library_table,
            log_retention=retention,
            removal_policy=removal,
        )
        # Agent deliveries and runtime job templates would block the destination and the queue; no VOD source can
        # appear once the Register step is gone.
        self.reaper.run_before_deleting(self.vended_logs, self.pipeline.queue)
        self.reaper.run_after_deleting(self.pipeline.register)
        self.as_run = AsRunLogging(
            self,
            "AsRun",
            prefix=prefix,
            library=data.library_table,
            as_run_retention_days=config.observability.log_retention_days,
            log_retention=retention,
            removal_policy=removal,
            breaks=config.modules.ops.enabled,
            shared=config.observability.as_run is AsRunMode.SHARED,
        )
        external_ads = config.ads.url if config.ads.mode is AdsMode.EXTERNAL_VAST else None
        self.channels = tuple(
            self._channel(channel, config=config, data=data, ads_url=external_ads) for channel in config.channels.iac
        )
        channels = tuple(channel.exports for channel in self.channels)

        self.observability = MediaObservability(
            self,
            "Observability",
            prefix=prefix,
            event_bus=self.events,
            state_machines={
                "ingest": self.pipeline.state_machine,
                "ladder provisioning": self.provisioner.state_machine,
            },
            mediaconvert_queue_arn=self.pipeline.queue_arn,
            channels=channels,
            distribution=self.distribution,
            log_retention=retention,
            removal_policy=removal,
            notification_email=config.notifications.email,
            trace_log_groups=self._trace_log_groups(),
            dead_letter_queues={"ingest": self.pipeline.dead_letter_queue},
            census_changes=config.modules.ops.enabled,
        )
        self.exports = MediaExports(
            distribution=self.distribution,
            cdn_base_url=self.delivery.cdn_base_url,
            content_base_url=content_base_url,
            event_bus=self.events.bus,
            alert_topic=self.observability.topic,
            ingest_state_machine=self.pipeline.state_machine,
            ladder_provisioner=self.provisioner.state_machine,
            mediaconvert_queue_arn=self.pipeline.queue_arn,
            source_location_name=self.origin.name,
            default_ladder_version=default_ladder_version,
            channels=channels,
            playback_configuration_template=playback_configuration_template(
                self,
                content_base_url=content_base_url,
                log_destination_arn=self.vended_logs.destination_arn,
                log_percent=config.observability.mediatailor_log_percent,
                ads_url=external_ads,
                sibling_policy=config.modules.release.enabled,
            ),
            mediatailor_log_destination_arn=self.vended_logs.destination_arn,
        )
        self._outputs()

    def _channel(
        self, channel: ChannelConfig, *, config: FastChannelsConfig, data: DataExports, ads_url: str | None
    ) -> LinearChannel:
        kit = self.kits[channel.ladder]
        linear = LinearChannel(
            self,
            f"Channel-{channel.name}",
            name=channel.name,
            ladder_version=kit.ladder_version,
            outputs=channel.outputs,
            filler_slate=kit.filler_vod_source,
            origin=self.origin,
            vended_logs=self.vended_logs,
            library=data.library_table,
            content_base_url=self.delivery.content_base_url,
            log_retention=config.observability.log_retention,
            ads_url=ads_url,
            tier=channel.tier,
            brief=channel.brief,
            log_percent=config.observability.mediatailor_log_percent,
            siblings=config.modules.release.enabled and channel.name in config.modules.release.channels,
            removal_policy=config.removal_policy,
        )
        # No dependency on AsRunLogging: channels are created stopped, so they log nothing before the stack is
        # complete, and the as-run resource tolerates a log group that MediaTailor created first.
        linear.node.add_dependency(kit)  # its filler slate and VAST pod
        self.reaper.run_after_deleting(linear)  # a filler slate in use cannot be deleted (observed)
        return linear

    def _trace_log_groups(self) -> list[logs.ILogGroup]:
        """Log groups the saved trace queries search besides the bus and MediaConvert logs: the pipeline's state
        machines and steps, MediaTailor's vended logs and the as-run log (``Trace program``)."""
        pipeline = self.pipeline
        functions = (pipeline.trigger, pipeline.prepare, pipeline.finalize, pipeline.register, pipeline.publish)
        return [
            pipeline.log_group,
            self.provisioner.log_group,
            *(function.log_group for function in (*functions, self.provisioner.kit)),
            self.vended_logs.log_group,
            logs.LogGroup.from_log_group_name(self, "AsRunLog", AS_RUN_LOG_GROUP),
            self.as_run.reconciler.log_group,
        ]

    def _outputs(self) -> None:
        """Values for scripts, e2e tests and operators (``scripts/_stack.py``); the Operator stack consumes
        :attr:`exports` instead. Constructs never create outputs, so any of them can be used twice in a stack."""
        exports = self.exports
        described = {
            "CdnBaseUrl": (exports.cdn_base_url, "Base URL of playback and media through CloudFront"),
            "DistributionId": (self.distribution.distribution_id, "Media CloudFront distribution"),
            "PathContract": (self.to_json_string(self.delivery.path_contract), "Routes for configuring another CDN"),
        }
        for name, (value, description) in described.items():
            CfnOutput(self, name, value=value, description=description)
        outputs = {
            "ContentBaseUrl": exports.content_base_url,
            "EventBusName": exports.event_bus.event_bus_name,
            "IngestStateMachineArn": exports.ingest_state_machine.state_machine_arn,
            "LadderProvisionerArn": exports.ladder_provisioner.state_machine_arn,
            "SourceLocationName": exports.source_location_name,
            "DefaultLadderVersion": exports.default_ladder_version,
            "Channels": self.to_json_string(
                [
                    {
                        "name": channel.name,
                        "ladderVersion": channel.ladder_version,
                        "outputs": list(channel.outputs),
                        "playback": channel.playback(exports.cdn_base_url),
                    }
                    for channel in exports.channels
                ]
            ),
            "DashboardUrl": (
                f"https://{self.region}.console.aws.amazon.com/cloudwatch/home?region={self.region}"
                f"#dashboards/dashboard/{self.observability.dashboard.dashboard_name}"
            ),
            "DashboardName": self.observability.dashboard.dashboard_name,
        }
        for name, value in outputs.items():
            CfnOutput(self, name, value=value)


def _kit_ladders(config: FastChannelsConfig) -> list[str]:
    """Built-in ladders whose kit is provisioned at deploy time: the default ladder and every IaC channel's."""
    used = {config.ladders.default, *(channel.ladder for channel in config.channels.iac)}
    return [name for name in config.ladders.builtin if name in used]
