"""What each stack of a deployment hands to the stacks after it: references and plain values only.

Constructs and stacks of every plane import these from here, so a construct such as ``LinearChannel`` or
``MediaObservability`` can be reused without the stack modules. ``stacks.data`` and ``stacks.media`` re-export them.
"""

from dataclasses import dataclass
from typing import Final

from aws_cdk import Fn, Token
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn

from fast_channels.core.specs import playback_urls

_PREFIX_PARTS: Final = 8
"""``https://<id>.mediatailor.<r>.amazonaws.com/v1/{master|dash}/<hash>/<config>/`` split on ``/``
(ad-insertion.md §1): indexes 3 to 6 are the path."""


@dataclass(frozen=True, slots=True)
class DataExports:
    """References to DataStack's resources, for the consumer stacks (Media, Operator, Edge).

    They are imported from L1 attribute tokens, which guarantees two things. Consumers get weak
    ``Fn::GetStackOutput`` references, which also work across regions (EdgeStack runs in us-east-1). And nothing a
    consumer does (grants, ``S3BucketOrigin.with_origin_access_control``) can add a statement to a DataStack
    resource policy, so no cross-stack cycle can form; grants land on the consumer's own principals.
    """

    ingest_bucket: s3.IBucket
    media_bucket: s3.IBucket
    logs_bucket: s3.IBucket
    library_table: dynamodb.ITableV2
    catalog_table: dynamodb.ITableV2
    memory: agentcore.IMemory | None
    """AgentCore Memory of the scheduler agent; ``None`` when ``agent.enabled`` is false."""
    memory_preference_namespace: str
    """Namespace template of the Memory's USER_PREFERENCE strategy (the runtime's ``MEMORY_PREFERENCE_NAMESPACE``)."""


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

    def playback(self, cdn_base_url: str) -> dict[str, dict[str, str]]:
        """Viewer URLs per output through the CDN at ``cdn_base_url`` (``core.specs.playback_urls``).

        The prefixes are CloudFormation attributes: their path is cut out with ``Fn::Split``, so the core builder
        still composes the URLs.
        """
        return playback_urls(
            cdn_base=cdn_base_url,
            playback_prefix_hls=_resolvable_prefix(self.hls_prefix),
            playback_prefix_dash=_resolvable_prefix(self.dash_prefix),
            outputs=self.outputs,
        )


@dataclass(frozen=True, slots=True)
class MediaExports:
    """What the operator plane consumes from the media plane (``MediaStack.exports``)."""

    distribution: cloudfront.IDistribution
    cdn_base_url: str
    """``https://<CloudFront domain>`` (or the custom domain), no trailing slash."""
    content_base_url: str
    """CDN-neutral base URL of content and ad segments (``delivery.content_domain`` or ``cdn_base_url``)."""
    event_bus: events.IEventBus
    alert_topic: sns.ITopic
    """Topic of the media plane's alarms and failure events; the operator plane's alarms notify it too."""
    ingest_state_machine: sfn.IStateMachine
    ladder_provisioner: sfn.IStateMachine
    mediaconvert_queue_arn: str
    source_location_name: str
    default_ladder_version: str
    channels: tuple[ChannelExports, ...]
    playback_configuration_template: str
    """JSON of the shared playback-configuration settings that agent-created channels reuse
    (``core.specs.playback_configuration`` inputs other than the channel-specific ones)."""
    mediatailor_log_destination_arn: str
    """Vended-logs delivery destination that agent-created playback configurations deliver to."""


def channel_playback(cdn_base_url: str, channel: ChannelExports) -> dict[str, dict[str, str]]:
    """:meth:`ChannelExports.playback` as a function (kept for v3.0 imports)."""
    return channel.playback(cdn_base_url)


def _resolvable_prefix(prefix: str) -> str:
    if not Token.is_unresolved(prefix):
        return prefix
    return "https://mediatailor/" + Fn.join("/", Fn.split("/", prefix, _PREFIX_PARTS)[3:7])
