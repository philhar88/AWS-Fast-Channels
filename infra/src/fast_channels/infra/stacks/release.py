"""``FastChannels-<prefix>-Release``: the release module (``modules.release``), beside the Prebid Server guidance."""

import json
from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_events as events
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_logs_destinations as logs_destinations
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm

from fast_channels.core.monetization import names
from fast_channels.core.pds import names as pds_names
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import alert_topic_arn
from fast_channels.infra.constructs.ops import ConfigurationAlarms, ZeroAdsDetector
from fast_channels.infra.constructs.ops.lake import LAKE_SUFFIX_PATH
from fast_channels.infra.constructs.pds.delivery import IMPRESSIONS
from fast_channels.infra.constructs.release import bundle as rendering
from fast_channels.infra.constructs.release.controller import ReleaseController
from fast_channels.infra.constructs.release.logs import ReleaseLogs
from fast_channels.infra.stacks.edge import EDGE_REGION

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.infra.config import FastChannelsConfig


class ReleaseStack(Stack):
    """The Release Controller and what it watches: the rendered bundle, the apply path, the drift check, the siblings'
    encrypted log group, their hook alarms and their zero-ad-sessions detector (DESIGN §8).

    Its bundle names the guidance's CloudFront domain, so ``scripts/prebid_guidance.py`` deploys it after the Pds
    stack and deletes it first; ``make deploy`` leaves it alone. It reads the Media stack's values by SSM parameter
    (``core.monetization.names.Parameter``), and the stage's bus, alert topic, ledger and lake by name.

    Args:
        config: deployment configuration (``modules.release``, the IaC channels, observability).
        network: the guidance's network (its CloudFront domain).
    """

    def __init__(
        self, scope: Construct, id: str, *, config: FastChannelsConfig, network: DemandNetwork, **kwargs: Any
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): Release Controller", **kwargs)
        Tags.of(self).add(names.MODULE_TAG, names.MODULE)
        prefix, release = config.prefix, config.modules.release
        retention, removal = config.observability.log_retention, RemovalPolicy.DESTROY

        def parameter(name: str) -> str:
            return ssm.StringParameter.value_for_string_parameter(self, name)

        self.rendered = rendering.render(
            config,
            pbs_origin=f"https://{network.cloudfront_domain}",
            content_base_url=parameter(names.parameter_name(prefix, names.Parameter.CONTENT_BASE_URL)),
            channel_urls={c: parameter(names.channel_parameter_name(prefix, c)) for c in release.channels},
        )
        topic = sns.Topic.from_topic_arn(self, "Alerts", alert_topic_arn(self, prefix))
        self.logs = ReleaseLogs(self, "Logs", prefix=prefix, retention=retention, removal_policy=removal)
        lake_ads, lake_manifest = names.lake_destinations(prefix)
        s3_layout = {"suffixPath": LAKE_SUFFIX_PATH, "enableHiveCompatiblePath": False}
        lake = {
            log_type: {"destination": arn(self, "logs", "delivery-destination", name, colon=True), "s3": s3_layout}
            for log_type, name in (("AD_DECISION_SERVER_LOGS", lake_ads), ("MANIFEST_SERVICE_LOGS", lake_manifest))
        }
        self.controller = ReleaseController(
            self,
            "Controller",
            prefix=prefix,
            channels=release.channels,
            bundle=self.rendered.document,
            event_bus=events.EventBus.from_event_bus_name(self, "Bus", f"{prefix}-fast-channels"),
            topic=topic,
            ledger_table=f"{prefix}-ledger",
            media_bucket=parameter(names.parameter_name(prefix, names.Parameter.MEDIA_BUCKET)),
            cdn_base_url=parameter(names.parameter_name(prefix, names.Parameter.CDN_BASE_URL)),
            log_destination_arn=self.logs.destination_arn,
            lake_deliveries=self.to_json_string(lake),
            origin_secret_region=EDGE_REGION,
            log_retention=retention,
            removal_policy=removal,
        )
        self.sibling_alarms = ConfigurationAlarms(
            self,
            "SiblingAlarms",
            configurations=[sibling.name for sibling in self.rendered.bundle.siblings],
            topic=topic,
        )
        self.zero_ads = ZeroAdsDetector(
            self,
            "ZeroAds",
            prefix=prefix,
            ads_log_group=names.log_group(prefix),
            topic=topic,
            log_retention=retention,
            removal_policy=removal,
            name="release-zero-ads-sessions",
        )
        self.logs.grant_read(self.zero_ads.function.function.role)  # type: ignore[arg-type]
        self.impressions = self._impressions(prefix)
        outputs = {
            "StateMachineArn": (self.controller.state_machine.state_machine_arn, "The apply path"),
            "BundleBucketName": (self.controller.bucket.bucket_name, "Release bundles (release/<digest>/bundle.json)"),
            "StateTableName": (self.controller.table.table_name, "The Release Controller's state"),
            "DriftFunction": (self.controller.drift.function.function_name, "The drift check (hourly)"),
            "SiblingLogGroup": (self.logs.log_group.log_group_name, "The siblings' MediaTailor logs (KMS)"),
            "Siblings": (json.dumps([s.name for s in self.rendered.bundle.siblings]), "Sibling configurations"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)

    def _impressions(self, prefix: str) -> logs.SubscriptionFilter:
        """The Pod Decision Service's delivery function also counts the siblings' impressions: the same filter as on
        the stage's log group (``IMPRESSIONS``), on the module's. Imported by ARN as in this environment, so the
        filter's invoke permission is created (a token ARN would leave its account unknown and skip it)."""
        delivery = lambda_.Function.from_function_attributes(
            self,
            "PdsDelivery",
            function_arn=ssm.StringParameter.value_for_string_parameter(
                self, pds_names.parameter_name(prefix, pds_names.Parameter.DELIVERY_FUNCTION)
            ),
            same_environment=True,
        )
        return logs.SubscriptionFilter(
            self,
            "Impressions",
            log_group=self.logs.log_group,
            destination=logs_destinations.LambdaDestination(delivery),
            filter_pattern=IMPRESSIONS,
            filter_name=f"{prefix}-pds-delivery-release",
        )
