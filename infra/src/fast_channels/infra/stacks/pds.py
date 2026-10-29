"""``FastChannels-<prefix>-Pds``: the Pod Decision Service (``modules.pds``), in the Prebid Server guidance's VPC."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, RemovalPolicy, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_sns as sns

from fast_channels.core.pbs_config import pds_host
from fast_channels.core.pds import names
from fast_channels.core.pds.records import PolicyRecord
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, SnapshotBase, demo_demand
from fast_channels.infra.config import DemandMode
from fast_channels.infra.constructs.alerts import alert_topic_arn, dead_letter_alarm
from fast_channels.infra.constructs.observability import census_parameter_name
from fast_channels.infra.constructs.pds import (
    DecisionLog,
    PdsCensusGate,
    PdsFunction,
    PdsService,
    SnapshotPublisher,
)
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_route53 as route53
    from constructs import Construct

    from fast_channels.core.demand import DemandNetwork
    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports

POLICY_HANDLER = "fast_channels.functions.pds.policy.handler"


def snapshot_base(config: FastChannelsConfig) -> SnapshotBase:
    """What the deployment puts in the snapshot: in demo mode, the guidance's bidder simulator as the demand."""
    demo = config.modules.demand.mode is DemandMode.DEMO
    return SnapshotBase(demand=demo_demand(config.prefix) if demo else {})


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
        data: the Data stack (the lake bucket, the PDS table and config bucket).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        network: DemandNetwork,
        zone: route53.IHostedZone,
        data: DataExports,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): Pod Decision Service", **kwargs)
        if data.lake_bucket is None or data.pds_table is None or data.pds_config_bucket is None:
            raise ValueError("the Pds stack needs the Data stack's lake, PDS table and config bucket")
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
        bucket = data.pds_config_bucket
        self.publisher = SnapshotPublisher(
            self,
            "Publisher",
            prefix=prefix,
            bucket=bucket,
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
        self.policy_records = DecisionLog(
            self,
            "PolicyRecords",
            prefix=prefix,
            kind="policy",
            model=PolicyRecord,
            log_group=self.policy.function.log_group,
            lake_bucket=data.lake_bucket,
            retention_days=config.modules.ops.lake_retention_days,
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
            functions=[self.policy],
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
            "PublisherFunction": (self.publisher.function.function.function_name, "Publishes the snapshot"),
            "ConfigBucketName": (bucket.bucket_name, "The PDS's private config bucket (snapshot/current.json)"),
            "TableName": (data.pds_table.table_name, "The PDS's table"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
