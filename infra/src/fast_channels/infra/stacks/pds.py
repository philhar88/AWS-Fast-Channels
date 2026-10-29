"""``FastChannels-<prefix>-Pds``: the Pod Decision Service (``modules.pds``), in the Prebid Server guidance's VPC."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm

from fast_channels.core.ledger import ledger_table
from fast_channels.core.pbs_config import pds_host
from fast_channels.core.pds import names
from fast_channels.core.pds.book import Line
from fast_channels.core.pds.records import DeliveryRecord, PolicyRecord, SelectRecord
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, SnapshotBase, demo_demand
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.infra.config import DemandMode
from fast_channels.infra.constructs.alerts import alert_topic_arn, dead_letter_alarm
from fast_channels.infra.constructs.observability import census_parameter_name
from fast_channels.infra.constructs.pds import (
    DecisionLog,
    DeliveryCounter,
    PdsCensusGate,
    PdsFunction,
    PdsService,
    ReleaseSweeper,
    SnapshotPublisher,
)
from fast_channels.infra.constructs.pds.alarms import ModuleAlarms
from fast_channels.infra.constructs.pds.delivery import allow_ledger, allow_table
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_route53 as route53
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.infra.config import FastChannelsConfig

POLICY_HANDLER = "fast_channels.functions.pds.policy.handler"
OWN_BOOK_HANDLER = "fast_channels.functions.pds.ownbook.handler"
SELECT_HANDLER = "fast_channels.functions.pds.select.handler"


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
    """The Pod Decision Service: its internal load balancer at ``pds.<prefix>.fast.internal`` with ``POST /policy``,
    the snapshot publisher, the decision records into the lake, and the census gate that keeps the functions warm
    while channels run. Its state (the table ``<prefix>-pds`` and the private config bucket) is the Data stack's.

    Like the Demand stack it needs the guidance's network, so ``scripts/prebid_guidance.py`` deploys it after the
    Demand stack (whose private zone it adds a record to) and deletes it before. ``make deploy`` leaves it alone.

    Args:
        config: deployment configuration (``modules.pds``, ``modules.demand.mode``, log retention, removal).
        network: the guidance's VPC, private subnets and task security group.
        zone: the stage's private zone (the Demand stack's).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        network: DemandNetwork,
        zone: route53.IHostedZone,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): Pod Decision Service", **kwargs)
        Tags.of(self).add(names.MODULE_TAG, names.MODULE)
        prefix, pds = config.prefix, config.modules.pds
        # Its log groups and queues go with the stack (like the Demand stack's): destroy-demand leaves nothing of it.
        retention, removal = config.observability.log_retention, RemovalPolicy.DESTROY
        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "GuidanceVpc",
            vpc_id=network.vpc_id,
            availability_zones=[subnet.az for subnet in network.private_subnets],
            private_subnet_ids=[subnet.id for subnet in network.private_subnets],
            private_subnet_route_table_ids=[subnet.route_table for subnet in network.private_subnets],
        )
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
        self.service = PdsService(
            self, "Service", prefix=prefix, vpc=vpc, task_security_group_id=network.task_security_group_id, zone=zone
        )
        self.policy = PdsFunction(
            self,
            "Policy",
            handler=POLICY_HANDLER,
            description="PDS POST /policy: the privacy mode and the bid-request patch of one break",
            environment={"FAST_PREFIX": prefix, "PDS_CONFIG_BUCKET": bucket.bucket_name},
            log_retention=retention,
            removal_policy=removal,
        )
        self.policy.function.allow(["s3:GetObject"], [bucket.arn_for_objects(SNAPSHOT_KEY)])
        self.service.add_endpoint(names.Endpoint.POLICY, self.policy)
        self.own_book = PdsFunction(
            self,
            "OwnBook",
            handler=OWN_BOOK_HANDLER,
            description="PDS POST /ownbook/openrtb: the own book's bids (house ads and direct lines)",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_CONFIG_BUCKET": bucket.bucket_name,
                "PDS_TABLE": names.table_name(prefix),
            },
            log_retention=retention,
            removal_policy=removal,
        )
        self.own_book.function.allow(["s3:GetObject"], [bucket.arn_for_objects(SNAPSHOT_KEY)])
        allow_table(self.own_book.function, prefix, ["BatchGetItem"], ["BUDGET#*"])
        self.service.add_endpoint(names.Endpoint.OWN_BOOK, self.own_book)
        self.select = PdsFunction(
            self,
            "Select",
            handler=SELECT_HANDLER,
            description="PDS POST /select: the pod of one break, its line ads reserved, our trackers",
            environment={
                "FAST_PREFIX": prefix,
                "PDS_CONFIG_BUCKET": bucket.bucket_name,
                "PDS_TABLE": names.table_name(prefix),
                "LEDGER_TABLE": ledger_table(prefix),
                "CONTENT_BASE_URL": content_base_url,
            },
            log_retention=retention,
            removal_policy=removal,
        )
        self.select.function.allow(["s3:GetObject"], [bucket.arn_for_objects(SNAPSHOT_KEY)])
        allow_table(self.select.function, prefix, ["BatchGetItem"], ["BUDGET#*"])
        allow_table(self.select.function, prefix, ["UpdateItem"], ["BUDGET#*", "CAP#*"])
        allow_table(self.select.function, prefix, ["PutItem"], ["RES#*"])
        allow_ledger(self.select.function, prefix)
        self.service.add_endpoint(names.Endpoint.SELECT, self.select)
        self.policy_records = DecisionLog(
            self,
            "PolicyRecords",
            prefix=prefix,
            kind="policy",
            model=PolicyRecord,
            log_group=self.policy.function.log_group,
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
            # The siblings log to the release module's own log group: its stack subscribes this function to it.
            ssm.StringParameter(
                self,
                "DeliveryFunctionParameter",
                parameter_name=names.parameter_name(prefix, names.Published.DELIVERY_FUNCTION),
                string_value=self.delivery.function.function.function_arn,
                description="Read by the release module's stack: the impressions of the sibling configurations",
            )
        self.select_records = DecisionLog(
            self,
            "SelectRecords",
            prefix=prefix,
            kind="select",
            model=SelectRecord,
            log_group=self.select.function.log_group,
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
        self.sweeper = ReleaseSweeper(
            self,
            "Sweeper",
            prefix=prefix,
            group=self.publisher.group,
            alert_topic=alerts,
            log_retention=retention,
            removal_policy=removal,
        )
        dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal)
        dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=dead_letters,
            name=f"{prefix}-pds-dead-letters",
            description="A Census Changed event could not reach the PDS census gate: its functions may stay cold, or "
            "warm without channels",
            topic=alerts,
        )
        self.census_gate = PdsCensusGate(
            self,
            "CensusGate",
            prefix=prefix,
            functions=[self.policy, self.own_book, self.select],
            concurrency=pds.provisioned_concurrency,
            census_parameter=census_parameter_name(prefix),
            log_retention=retention,
            dead_letters=dead_letters,
        )
        outputs = {
            "CensusGateArn": (
                self.census_gate.state_machine.state_machine_arn,
                "Sets the functions' provisioned concurrency with the running channels",
            ),
            "Endpoint": (f"http://{pds_host(prefix)}", "The PDS's base URL in the Prebid Server VPC"),
            "PolicyFunction": (self.policy.function.function.function_name, "POST /policy"),
            "OwnBookFunction": (self.own_book.function.function.function_name, "POST /ownbook/openrtb"),
            "SelectFunction": (self.select.function.function.function_name, "POST /select"),
            "DeliveryFunction": (self.delivery.function.function.function_name, "Counts our tracker's impressions"),
            "SweeperFunction": (self.sweeper.function.function.function_name, "Releases undelivered reservations"),
            "PublisherFunction": (self.publisher.function.function.function_name, "Publishes the snapshot"),
            "ConfigBucketName": (bucket.bucket_name, "The PDS's private config bucket (snapshot/current.json)"),
            "TableName": (names.table_name(prefix), "The PDS's table (the Data stack's)"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
