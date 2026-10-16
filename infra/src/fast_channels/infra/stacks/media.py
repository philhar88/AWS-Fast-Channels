"""``FastChannels-Media``: the media plane (SPEC §5).

Skeleton: the constructs of CONTRACTS §F are added at the integration points below, in this order.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aws_cdk import Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_events as events
from aws_cdk import aws_stepfunctions as sfn

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.stacks.data import DataExports


@dataclass(frozen=True, slots=True)
class ChannelExports:
    """An IaC linear channel and its per-channel MediaTailor playback configuration."""

    name: str
    arn: str
    ladder_version: str
    outputs: tuple[str, ...]
    """Channel output (= source group = rendition profile) names, e.g. ``("hls", "hls-avc", "dash-avc")``."""
    playback_configuration_name: str
    hls_prefix: str
    """``HlsConfiguration.ManifestEndpointPrefix`` of the playback configuration (MediaTailor host)."""
    dash_prefix: str
    """``DashConfiguration.ManifestEndpointPrefix`` of the playback configuration (MediaTailor host)."""


@dataclass(frozen=True, slots=True)
class MediaExports:
    """What the operator plane consumes from the media plane (set as ``MediaStack.exports``)."""

    distribution: cloudfront.IDistribution
    cdn_base_url: str
    """``https://<CloudFront domain>`` (or the custom domain), no trailing slash."""
    content_base_url: str
    """CDN-neutral base URL of content and ad segments (``delivery.content_domain`` or ``cdn_base_url``)."""
    event_bus: events.IEventBus
    ingest_state_machine: sfn.IStateMachine
    ladder_provisioner: sfn.IStateMachine
    mediaconvert_role_arn: str
    mediaconvert_queue_arn: str
    source_location_name: str
    default_ladder_version: str
    channels: tuple[ChannelExports, ...]
    playback_configuration_template: str
    """JSON of the shared playback-configuration settings that agent-created channels reuse
    (``core.specs.playback_configuration`` inputs other than the channel-specific ones)."""
    mediatailor_log_destination_arn: str
    """Vended-logs delivery destination that agent-created playback configurations deliver to."""


class MediaStack(Stack):
    """Ingest pipeline, ladders and their kits, Channel Assembly origin, IaC channels with their playback
    configurations, CloudFront distribution, runtime-resource reaper and media observability.

    Args:
        config: deployment configuration (``ladders``, ``channel_assembly``, ``channels``, ``delivery``, ``ads``,
            ``observability``, ``notifications``).
        data: DataStack references; never add resource-policy statements to them (DataStack owns the policies).
    """

    def __init__(
        self, scope: Construct, id: str, *, config: FastChannelsConfig, data: DataExports, **kwargs: Any
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): media plane", **kwargs)
        # Remove once the first construct below exists: CloudFormation cannot deploy a stack without resources.
        nag.acknowledge(self, {nag.EMPTY_TEMPLATE: "Skeleton: its constructs are added at the integration points"})
        # Integration points (stage-2 media agents), every Lambda via constructs.functions.FastFunction:
        #
        # 1. delivery.MediaDistribution(media_bucket=data.media_bucket, delivery=config.delivery)
        #    -> distribution, content_base_url (delivery.content_domain or the CloudFront domain), path_contract.
        #    Expose `self.distribution`; the stage passes it to EdgeStack (regions other than us-east-1).
        #    delivery.CdnAccessLogs(distribution, logs_bucket=data.logs_bucket) is created here only when the
        #    stage has no EdgeStack (region us-east-1).
        # 2. channel_assembly.ChannelAssemblyOrigin(bucket=data.media_bucket, content_base_url): S3_SIGV4 source
        #    location named `<prefix>...` (DataStack's bucket policy allows `sourceLocation/<prefix>*`).
        #    channel_assembly.RuntimeResourceReaper: everything else depends on it so it is deleted first.
        # 3. encoding.IngestPipeline(ingest_bucket=data.ingest_bucket, media_bucket=data.media_bucket,
        #    library=data.library_table, origin): queue, MediaConvert role, state machine, `fast-channels` bus.
        #    encoding.EncodingLadder per config.ladders.builtin; encoding.LadderProvisioner; encoding.LadderKit.
        # 4. Per config.channels.iac: channel_assembly.AdInsertion + channel_assembly.LinearChannel;
        #    channel_assembly.AsRunLogging once.
        # 5. observability.MediaObservability(...): dashboard, alarms, MediaConvert job-state logs, saved queries.
