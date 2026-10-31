"""MediaTailor ad insertion: playback configurations and their shared vended-logs destination (SPEC §5.3)."""

from typing import Any, Final

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediatailor as mediatailor
from constructs import Construct

from fast_channels.core import specs
from fast_channels.core.naming import mediatailor_log_group
from fast_channels.infra.constructs.channel_assembly._cfn import override, with_tokens
from fast_channels.infra.constructs.logs import deliver, vended_log_group

VENDED_LOG_TYPES: Final = {
    "AD_DECISION_SERVER_LOGS": "ads",
    "MANIFEST_SERVICE_LOGS": "manifest",
    "TRANSCODE_LOGS": "transcode",
}
"""MediaTailor vended-log types -> delivery source name suffix (``<playback configuration>-<suffix>``)."""


class MediaTailorLogs(Construct):
    """The vended-logs destination of every MediaTailor playback configuration in the deployment.

    One CloudWatch Logs group ``/aws/vendedlogs/mediatailor/<prefix>`` behind one delivery destination; each
    playback configuration adds a delivery source and a delivery per log type (:meth:`deliver`; the agent's
    channels do the same through the API). The log group's own resource policy admits the account's delivery
    sources named ``<prefix>-*`` (:func:`~fast_channels.infra.constructs.logs.vended_log_group`).

    Args:
        prefix: deployment prefix; delivery sources must be named ``<prefix>-*``.
        log_retention: log retention.
        removal_policy: what happens to the log group when the stack is deleted.

    Attributes:
        log_group: the destination log group. destination_arn: ARN of the delivery destination
            (``MediaExports.mediatailor_log_destination_arn``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.log_group = vended_log_group(
            self,
            "Logs",
            name=mediatailor_log_group(prefix),
            delivery_sources=f"{prefix}-*",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self._destination = logs.CfnDeliveryDestination(
            self,
            "Destination",
            name=f"{prefix}-mediatailor",
            destination_resource_arn=self.log_group.log_group_arn,
            output_format="json",
        )
        self.destination_arn = self._destination.attr_arn
        self._last_delivery: logs.CfnDelivery | None = None

    def deliver(self, scope: Construct, playback_configuration: mediatailor.CfnPlaybackConfiguration) -> None:
        """Deliver the three log types of ``playback_configuration`` here, from delivery sources in ``scope``.

        Deliveries to one destination are created one after another (the research stack chained them too).
        """
        for log_type, suffix in VENDED_LOG_TYPES.items():
            source = logs.CfnDeliverySource(
                scope,
                f"{suffix.title()}LogSource",
                name=f"{playback_configuration.name}-{suffix}",
                log_type=log_type,
                resource_arn=playback_configuration.attr_playback_configuration_arn,
            )
            self._last_delivery = deliver(
                scope,
                f"{suffix.title()}LogDelivery",
                source=source,
                destination_arn=self.destination_arn,
                after=self._last_delivery,
            )


def playback_configuration_template(
    scope: Construct,
    *,
    content_base_url: str,
    log_destination_arn: str,
    log_percent: int,
    ads_url: str | None = None,
    sibling_policy: bool = False,
) -> str:
    """JSON of what agent-created playback configurations share (``MediaExports.playback_configuration_template``,
    the tools' ``PLAYBACK_CONFIG_TEMPLATE``)::

        {"content_base_url": str, "log_percent": int, "log_destination_arn": str,
         "log_types": {"AD_DECISION_SERVER_LOGS": "ads", "MANIFEST_SERVICE_LOGS": "manifest", "TRANSCODE_LOGS":
         "transcode"}, "ads_url": str,   # ads_url only with an external ADS
         "sibling_policy": true}          # only with the release module

    A tool builds a channel's configuration with ``core.specs.playback_configuration(name=<channel>,
    video_content_source_url=<channel base URL>, ads_url=template.get("ads_url") or core.specs.ads_url(...),
    content_base_url, log_percent, tags)``, applies it with ``core.specs.playback_configuration_requests`` and
    delivers each log type from a delivery source named ``<channel>-<log_types[type]>`` (at most 60 characters,
    so channel names of at most 50) to ``log_destination_arn`` (verified live with an agent-style channel).

    Args:
        content_base_url: CDN-neutral base URL of content and ads.
        log_destination_arn: :attr:`MediaTailorLogs.destination_arn`.
        log_percent: share of sessions that log (``observability.mediatailor_log_percent``).
        ads_url: the external ADS URL (``ads.mode = external_vast``); ``None`` for preconditioned ads.
        sibling_policy: agent channels' policies also admit sibling configurations ``<channel>-p-?``
            (``core.specs.channel_policy(siblings=True)``), so the release module can monetize them later without
            changing a policy (``modules.release.enabled``).
    """
    template: dict[str, Any] = {
        "content_base_url": content_base_url,
        "log_percent": log_percent,
        "ads_url": ads_url,
        "log_destination_arn": log_destination_arn,
        "log_types": VENDED_LOG_TYPES,
    }
    if sibling_policy:
        template["sibling_policy"] = True
    return Stack.of(scope).to_json_string(template)


class AdInsertion(Construct):
    """A MediaTailor playback configuration serving SSAI (``STITCHED_ONLY``) from one channel, with its vended logs.

    The configuration is ``core.specs.playback_configuration`` verbatim (server-side insertion only, preconditioned
    ads, all three personalization timeouts, absolute CDN prefixes, VENDED_LOGS), so IaC channels and agent channels
    are identical.

    Args:
        name: configuration name (the channel name by convention; the channel policy admits exactly this one).
        channel_url: Channel Assembly base URL of the channel (``https://<host>/v1/channel/<name>/``); MediaTailor
            signs requests to Channel Assembly hosts with SigV4 (channel-assembly.md §6.1).
        ads_url: ADS URL (``core.specs.ads_url`` for preconditioned ads); may contain CDK tokens.
        content_base_url: base URL of ad and content segments; may contain CDK tokens.
        vended_logs: the shared vended-logs destination.
        log_percent: share of sessions that log (100 for demos, about 10 in production).

    Attributes:
        playback_configuration: the ``CfnPlaybackConfiguration``. name, arn, hls_prefix: its name, ARN and
            HLS ``ManifestEndpointPrefix``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        name: str,
        channel_url: str,
        ads_url: str,
        content_base_url: str,
        vended_logs: MediaTailorLogs,
        log_percent: int = 100,
    ) -> None:
        super().__init__(scope, id)
        properties = specs.to_cfn_playback_configuration(
            with_tokens(
                specs.playback_configuration,
                name=name,
                video_content_source_url=channel_url,
                ads_url=ads_url,
                content_base_url=content_base_url,
                log_percent=log_percent,
                tags={},  # fast:app and fast:owner come from the stage's tags
            )
        )
        required = {key: properties.pop(key) for key in ("Name", "AdDecisionServerUrl", "VideoContentSourceUrl")}
        properties.pop("Tags")
        self.playback_configuration = mediatailor.CfnPlaybackConfiguration(
            self,
            "Resource",
            name=required["Name"],
            ad_decision_server_url=required["AdDecisionServerUrl"],
            video_content_source_url=required["VideoContentSourceUrl"],
        )
        override(self.playback_configuration, properties)
        vended_logs.deliver(self, self.playback_configuration)
        self.name = name
        self.arn = self.playback_configuration.attr_playback_configuration_arn
        self.hls_prefix = self.playback_configuration.attr_hls_configuration_manifest_endpoint_prefix
