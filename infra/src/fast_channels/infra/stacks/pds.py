"""``FastChannels-<prefix>-Pds``: the Pod Decision Service (``modules.pds``) beside the Prebid Server guidance: all of
it but the sidecar that answers Prebid Server, which runs in the guidance's task (ADR-0036)."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm

from fast_channels.core.pds import names
from fast_channels.core.pds.book import Line
from fast_channels.core.pds.records import DeliveryRecord, PolicyRecord, SelectRecord
from fast_channels.core.pds.snapshot import SnapshotBase, demo_demand
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.infra.config import DemandMode
from fast_channels.infra.constructs.alerts import alert_topic_arn
from fast_channels.infra.constructs.crawl.kpis import KPIS
from fast_channels.infra.constructs.ops.lake import lake_database_name, view_table
from fast_channels.infra.constructs.pds import DecisionLog, DeliveryCounter, ReleaseSweeper, SnapshotPublisher
from fast_channels.infra.constructs.pds.alarms import ModuleAlarms

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.infra.config import FastChannelsConfig


def snapshot_base(config: FastChannelsConfig) -> SnapshotBase:
    """What the deployment puts in the snapshot: the break lengths of the default ladder's grid and, in demo mode, the
    guidance's bidder simulator as the demand and the synthetic direct lines (``modules.pds.demo_lines``) in the own
    book."""
    grid = config.ladder(config.ladders.default).grid
    breaks_ms = tuple(grid.gop_exact(gops) for gops in BREAK_SLATE_GOPS)
    if config.modules.demand.mode is not DemandMode.DEMO:
        return SnapshotBase(breaks_ms=breaks_ms)
    lines = tuple(
        Line(
            id=line.id,
            advertiser=line.advertiser,
            adomain=line.adomain,
            cat=line.cat,
            creatives=line.creatives,
            cpm=line.cpm,
            budget=line.budget,
            per_session=line.caps.per_session,
            tier0=line.tier0,
        )
        for line in config.modules.pds.demo_lines
    )
    return SnapshotBase(demand=demo_demand(config.prefix), lines=lines, breaks_ms=breaks_ms)


class PdsStack(Stack):
    """The Pod Decision Service beside its sidecar: the snapshot publisher, the decision records into the lake (the
    sidecar's ``pds.policy`` and ``pds.select``, the delivery function's ``pds.delivery``), the delivery counter, the
    reservations' sweeper and the module's fallback alarms. Its state (the table ``<prefix>-pds`` and the private
    config bucket) is the Data stack's.

    The sidecar itself (``/policy``, ``/select``, ``/ownbook/openrtb`` on ``127.0.0.1:8090``) is a container of the
    guidance's Prebid Server task (``scripts/guidance_app.py``), whose stack creates the sidecar's log group. So
    ``scripts/prebid_guidance.py`` deploys this stack after the guidance and the Demand stack, and deletes it before.
    ``make deploy`` leaves it alone.

    Args:
        config: deployment configuration (``modules.pds``, ``modules.demand.mode``, log retention, removal).
        network: the guidance's network and its Prebid Server container's log group (the fallback alarms).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        network: DemandNetwork,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): Pod Decision Service", **kwargs)
        Tags.of(self).add(names.MODULE_TAG, names.MODULE)
        prefix = config.prefix
        # Its log groups and queues go with the stack (like the Demand stack's): destroy-demand leaves nothing of it.
        retention, removal = config.observability.log_retention, RemovalPolicy.DESTROY
        alerts = sns.Topic.from_topic_arn(self, "Alerts", alert_topic_arn(self, prefix))
        # The Data stack's state, by SSM parameter (resolved at deploy) and by name: no weak reference.
        bucket = s3.Bucket.from_bucket_name(
            self,
            "ConfigBucket",
            ssm.StringParameter.value_for_string_parameter(
                self, names.parameter_name(prefix, names.Parameter.CONFIG_BUCKET)
            ),
        )
        lake = s3.Bucket.from_bucket_name(
            self,
            "LakeBucket",
            ssm.StringParameter.value_for_string_parameter(
                self, names.parameter_name(prefix, names.Parameter.LAKE_BUCKET)
            ),
        )
        media = s3.Bucket.from_bucket_name(
            self,
            "MediaBucket",
            ssm.StringParameter.value_for_string_parameter(
                self, names.parameter_name(prefix, names.Parameter.MEDIA_BUCKET)
            ),
        )
        # The Media stack's CDN, by SSM parameter too (our tracker's host).
        content_base_url = ssm.StringParameter.value_for_string_parameter(
            self, names.parameter_name(prefix, names.Parameter.CONTENT_BASE_URL)
        )
        self.publisher = SnapshotPublisher(
            self,
            "Publisher",
            prefix=prefix,
            bucket=bucket,
            media_bucket=media,
            base=snapshot_base(config),
            alert_topic=alerts,
            log_retention=retention,
            removal_policy=removal,
        )
        crawl = config.modules.crawl
        # The sidecar's log group, which the guidance stack creates: its pds.policy and pds.select records.
        self.decision_log_group = logs.LogGroup.from_log_group_name(
            self, "SidecarLogs", names.sidecar_log_group(prefix)
        )
        self.policy_records = DecisionLog(
            self,
            "PolicyRecords",
            prefix=prefix,
            kind="policy",
            model=PolicyRecord,
            log_group=self.decision_log_group,
            lake_bucket=lake,
            retention_days=config.modules.ops.lake_retention_days,
            log_retention=retention,
            removal_policy=removal,
        )
        self.delivery = DeliveryCounter(
            self,
            "Delivery",
            prefix=prefix,
            content_base_url=content_base_url,
            alert_topic=alerts,
            log_retention=retention,
            removal_policy=removal,
        )
        if config.modules.release.enabled:
            self._publish_for_release(prefix)
        self.select_records = DecisionLog(
            self,
            "SelectRecords",
            prefix=prefix,
            kind="select",
            model=SelectRecord,
            log_group=self.decision_log_group,
            lake_bucket=lake,
            retention_days=config.modules.ops.lake_retention_days,
            log_retention=retention,
            removal_policy=removal,
        )
        self.module_alarms = (
            ModuleAlarms(
                self,
                "ModuleAlarms",
                prefix=prefix,
                log_group_name=network.container_log_group,
                alert_topic=alerts,
            )
            if network.container_log_group
            else None
        )
        self.delivery_records = DecisionLog(
            self,
            "DeliveryRecords",
            prefix=prefix,
            kind="delivery",
            model=DeliveryRecord,
            log_group=self.delivery.function.log_group,
            lake_bucket=lake,
            retention_days=config.modules.ops.lake_retention_days,
            log_retention=retention,
            removal_policy=removal,
        )
        self.crawl_kpis = None
        if crawl.enabled:
            # The crawl module's KPIs, a view over the three tables of decision records.
            self.crawl_kpis = view_table(self, "CrawlKpis", KPIS, database_name=lake_database_name(prefix))
            for records in (self.policy_records, self.select_records, self.delivery_records):
                self.crawl_kpis.node.add_dependency(records.table)
        self.sweeper = ReleaseSweeper(
            self,
            "Sweeper",
            prefix=prefix,
            group=self.publisher.group,
            alert_topic=alerts,
            log_retention=retention,
            removal_policy=removal,
        )
        outputs = {
            "DecisionLogGroup": (
                self.decision_log_group.log_group_name,
                "The sidecar's log group: its pds.policy and pds.select decision records",
            ),
            "DeliveryFunction": (self.delivery.function.function.function_name, "Counts our tracker's impressions"),
            "SweeperFunction": (self.sweeper.function.function.function_name, "Releases undelivered reservations"),
            "PublisherFunction": (self.publisher.function.function.function_name, "Publishes the snapshot"),
            "ConfigBucketName": (bucket.bucket_name, "The PDS's private config bucket (snapshot/current.json)"),
            "TableName": (names.table_name(prefix), "The PDS's table (the Data stack's)"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)

    def _publish_for_release(self, prefix: str) -> None:
        """The parameters the release module's stack reads."""
        # The siblings log to the release module's own log group: its stack subscribes this function to it.
        ssm.StringParameter(
            self,
            "DeliveryFunctionParameter",
            parameter_name=names.parameter_name(prefix, names.Published.DELIVERY_FUNCTION),
            string_value=self.delivery.function.function.function_arn,
            description="Read by the release module's stack: the impressions of the sibling configurations",
        )
        ssm.StringParameter(
            self,
            "PolicyLogGroupParameter",
            parameter_name=names.parameter_name(prefix, names.Published.POLICY_LOG_GROUP),
            string_value=self.decision_log_group.log_group_name,
            description="Read by the release module's stack: its smoke test counts the decision records",
        )
