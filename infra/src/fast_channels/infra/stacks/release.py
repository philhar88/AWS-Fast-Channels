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

from fast_channels.core.crawl.functions import CrawlContribution
from fast_channels.core.monetization import names
from fast_channels.core.monetization.playback import SiblingRole
from fast_channels.core.monetization.prefetch import PrefetchPolicy
from fast_channels.core.pds import names as pds_names
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import alert_topic_arn
from fast_channels.infra.constructs.ops import ConfigurationAlarms, ZeroAdsDetector
from fast_channels.infra.constructs.ops.lake import LAKE_SUFFIX_PATH
from fast_channels.infra.constructs.pds.delivery import IMPRESSIONS
from fast_channels.infra.constructs.release import bundle as rendering
from fast_channels.infra.constructs.release.controller import PointerSettings, ReleaseController, SmokeSettings
from fast_channels.infra.constructs.release.detectors import LogReconciler, MarkerDetector
from fast_channels.infra.constructs.release.logs import ReleaseLogs
from fast_channels.infra.constructs.release.pointer import ReleasePointer
from fast_channels.infra.constructs.release.prefetch import Prefetcher
from fast_channels.infra.stacks.edge import EDGE_REGION

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.core.monetization.bundle import BundleContribution
    from fast_channels.infra.config import FastChannelsConfig


def contributions(config: FastChannelsConfig) -> list[BundleContribution]:
    """The other modules' function trees in the bundle: the crawl module's PRE_ADS content lookup when
    ``modules.crawl.enabled`` and ``pre_ads``."""
    crawl = config.modules.crawl
    return [CrawlContribution()] if crawl.enabled and crawl.pre_ads else []


def break_lengths_ms(config: FastChannelsConfig) -> tuple[int, ...]:
    """Every break length of the monetized channels: their ladders' slate grids."""
    ladders = {c.ladder for c in config.channels.iac if c.name in config.modules.release.channels}
    return tuple(sorted({config.ladder(name).grid.gop_exact(gops) for name in ladders for gops in BREAK_SLATE_GOPS}))


def prefetch_policy(config: FastChannelsConfig) -> PrefetchPolicy:
    """``modules.release.prefetch``'s timing as the prefetcher reads it."""
    prefetch = config.modules.release.prefetch
    return PrefetchPolicy(
        horizon_breaks=prefetch.horizon_breaks,
        retrieval_lead_s=prefetch.retrieval_lead_seconds,
        retrieval_end_s=prefetch.retrieval_end_seconds,
        shaping_window_s=prefetch.shaping_window_seconds,
    )


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
        stage_id = self.stack_name.removesuffix("-Release")  # FastChannels-<prefix>
        retention, removal = config.observability.log_retention, RemovalPolicy.DESTROY

        def parameter(name: str) -> str:
            return ssm.StringParameter.value_for_string_parameter(self, name)

        self.rendered = rendering.render(
            config,
            pbs_origin=f"https://{network.cloudfront_domain}",
            content_base_url=parameter(names.parameter_name(prefix, names.Parameter.CONTENT_BASE_URL)),
            channel_urls={c: parameter(names.channel_parameter_name(prefix, c)) for c in config.release_channels},
            contributions=contributions(config),
        )
        topic = sns.Topic.from_topic_arn(self, "Alerts", alert_topic_arn(self, prefix))
        self.logs = ReleaseLogs(self, "Logs", prefix=prefix, retention=retention, removal_policy=removal)
        lake_ads, lake_manifest = names.lake_destinations(prefix)
        s3_layout = {"suffixPath": LAKE_SUFFIX_PATH, "enableHiveCompatiblePath": False}
        lake = {
            log_type: {"destination": arn(self, "logs", "delivery-destination", name, colon=True), "s3": s3_layout}
            for log_type, name in (("AD_DECISION_SERVER_LOGS", lake_ads), ("MANIFEST_SERVICE_LOGS", lake_manifest))
        }
        bus = events.EventBus.from_event_bus_name(self, "Bus", f"{prefix}-fast-channels")
        siblings = [sibling.name for sibling in self.rendered.bundle.siblings]
        pair = [s.name for s in self.rendered.bundle.siblings if s.role is SiblingRole.GENERAL]
        self.sibling_alarms = ConfigurationAlarms(self, "SiblingAlarms", configurations=siblings, topic=topic)
        watched = [*siblings, *([names.lab_check(prefix)] if config.lab_channel else [])]
        self.markers = MarkerDetector(self, "Markers", log_group=self.logs.log_group, siblings=watched, topic=topic)
        application = parameter(names.parameter_name(prefix, names.Parameter.APPCONFIG_APPLICATION))
        self.pointer = ReleasePointer(
            self,
            "Pointer",
            prefix=prefix,
            application_id=application,
            rollout=self.rendered.bundle.rollout,
            alarms=[
                *(a for name, a in self.sibling_alarms.alarms.items() if name.startswith(tuple(pair))),
                *(self.markers.alarms[name] for name in pair),
            ],
            topic=topic,
        )
        self.controller = ReleaseController(
            self,
            "Controller",
            prefix=prefix,
            channels=config.release_channels,
            bundle=self.rendered.document,
            event_bus=bus,
            topic=topic,
            ledger_table=f"{prefix}-ledger",
            media_bucket=parameter(names.parameter_name(prefix, names.Parameter.MEDIA_BUCKET)),
            cdn_base_url=parameter(names.parameter_name(prefix, names.Parameter.CDN_BASE_URL)),
            log_destination_arn=self.logs.destination_arn,
            lake_deliveries=self.to_json_string(lake),
            origin_secret_region=EDGE_REGION,
            pointer=PointerSettings(
                application=application,
                environment=parameter(names.parameter_name(prefix, names.Parameter.APPCONFIG_ENVIRONMENT)),
                profile=self.pointer.profile.ref,
                strategy=self.pointer.strategy.ref,
                at_once=self.pointer.at_once.ref,
            ),
            smoke=SmokeSettings(
                decision_log_group=parameter(pds_names.parameter_name(prefix, pds_names.Published.POLICY_LOG_GROUP)),
                tool_functions=parameter(names.parameter_name(prefix, names.Parameter.TOOL_FUNCTIONS))
                if config.agent.enabled
                else "",
                tool_function_prefix=f"{stage_id}-Operator-" if config.agent.enabled else "",
            ),
            rollout_minutes=self.rendered.bundle.rollout.deployment_duration_in_minutes
            + self.rendered.bundle.rollout.final_bake_time_in_minutes,
            log_retention=retention,
            removal_policy=removal,
        )
        self.prefetcher = (
            Prefetcher(
                self,
                "Prefetcher",
                prefix=prefix,
                channels=release.channels,
                letters=release.prefetch.siblings,
                policy=prefetch_policy(config),
                break_lengths_ms=break_lengths_ms(config),
                state_table=self.controller.table.table_name,
                event_bus=bus,
                alert_topic=topic,
                log_retention=retention,
                removal_policy=removal,
            )
            if release.prefetch.enabled
            else None
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
        self.reconciler = LogReconciler(
            self,
            "LogReconciler",
            prefix=prefix,
            siblings=siblings,
            environment={"FAST_PREFIX": prefix},
            topic=topic,
            log_retention=retention,
        )
        self.logs.grant_read(self.reconciler.function.function.role)  # type: ignore[arg-type]
        self.impressions = self._impressions(prefix)
        self.logs.grant_read(self.controller.smoke.function.role)  # type: ignore[arg-type]
        outputs = {
            "StateMachineArn": (self.controller.state_machine.state_machine_arn, "The apply path"),
            "BundleBucketName": (self.controller.bucket.bucket_name, "Release bundles (release/<digest>/bundle.json)"),
            "BundleKey": (self.controller.desired.get_att_string("BundleKey"), "The bundle this deployment rendered"),
            "StateTableName": (self.controller.table.table_name, "The Release Controller's state"),
            "DriftFunction": (self.controller.drift.function.function_name, "The drift check (hourly)"),
            "SiblingLogGroup": (self.logs.log_group.log_group_name, "The siblings' MediaTailor logs (KMS)"),
            "Siblings": (json.dumps([s.name for s in self.rendered.bundle.siblings]), "Sibling configurations"),
        }
        if self.prefetcher is not None:
            outputs["PrefetchFunction"] = (
                self.prefetcher.function.function.function_name,
                "The prefetcher (every 5 minutes, after a re-plan or a channel start)",
            )
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
                self, pds_names.parameter_name(prefix, pds_names.Published.DELIVERY_FUNCTION)
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
