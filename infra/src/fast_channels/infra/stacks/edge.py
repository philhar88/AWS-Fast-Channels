"""``FastChannels-Edge-<region>``: CloudFront resources that must live in us-east-1 when a deployment runs in
``<region>``."""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Stack

from fast_channels.infra.constructs.delivery import CdnAccessLogs
from fast_channels.infra.constructs.observability import CLOUDFRONT_REGION, alert_topic, cdn_error_alarm

if TYPE_CHECKING:
    from aws_cdk import aws_cloudfront as cloudfront
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.stacks.data import DataExports

EDGE_REGION: Final = CLOUDFRONT_REGION
"""Region of CloudFront's control plane: standard-logs v2 delivery sources, metrics (and so alarms), ACM
certificates, WAF web ACLs."""


class EdgeStack(Stack):
    """The media distribution's standard-logs-v2 delivery (``delivery.CdnAccessLogs``) and 5xx alarm with its own
    alert topic, when the stage's region is not us-east-1; in us-east-1 the Media stack holds them and this stack
    is not created.

    Cross-region wiring: with weak cross-stack references (cdk.json ``@aws-cdk/core:defaultCrossStackReferences``)
    a value from the deployment region resolves to ``Fn::GetStackOutput {StackName, Region, OutputName}``; no
    ``cross_region_references`` custom resources. That works for L1 attribute tokens: :class:`DataExports`
    references and ``Distribution.distribution_arn``/``distribution_id``. The delivery source (us-east-1) writes
    to the logs bucket in the deployment region; S3 log delivery crosses regions (verified with us-west-2).

    Names carry the deployment region (``<prefix>-<region>-…``): the same prefix may also be deployed in
    us-east-1, whose Media stack then owns ``<prefix>-cdn-access`` and ``<prefix>-cdn-5xx``.

    Args:
        config: deployment configuration (``prefix``, ``observability``, ``notifications``, ``removal``).
        data: DataStack references (``logs_bucket`` receives the CloudFront logs).
        distribution: the Media stack's distribution.

    Attributes:
        access_logs: the CloudFront log delivery.
        topic: alert topic of the CloudFront alarm (alarms publish to topics of their own region).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        distribution: cloudfront.IDistribution,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            scope, id, description=f"FAST Channels ({config.prefix}): CloudFront logs and alarms", **kwargs
        )
        name = f"{config.prefix}-{distribution.stack.region}"
        self.access_logs = CdnAccessLogs(
            self,
            "AccessLogs",
            distribution=distribution,
            logs_bucket=data.logs_bucket,
            name=name,
            removal_policy=config.removal_policy,
        )
        self.topic = alert_topic(self, "Alerts", name=f"{name}-alerts", email=config.notifications.email)
        cdn_error_alarm(self, "CdnErrors", distribution=distribution, topic=self.topic, name=f"{name}-cdn-5xx")
