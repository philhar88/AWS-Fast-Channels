"""``FastChannels-<prefix>-Demand``: what runs beside the Prebid Server guidance, in its VPC."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm

from fast_channels.core import specs
from fast_channels.core.ad_server import AdServer
from fast_channels.core.demand import MODULE, MODULE_TAG, DemandNetwork, Parameter, parameter_path
from fast_channels.core.monetization import names as release_names
from fast_channels.core.pbs_config import CACHE_PATH, TRACKER_PATH
from fast_channels.infra.config import DemandMode, Simulator
from fast_channels.infra.constructs.alerts import alert_topic_arn, dead_letter_alarm
from fast_channels.infra.constructs.demand.ad_server import CacheRecord, MockAdServer
from fast_channels.infra.constructs.demand.private_dns import PrivateDns
from fast_channels.infra.constructs.demand.scaling import CensusGate, Dayparts
from fast_channels.infra.constructs.demand.simulator import LambdaSimulator
from fast_channels.infra.constructs.demand.warmup import WarmUp
from fast_channels.infra.constructs.observability import census_parameter_name
from fast_channels.infra.constructs.queues import dead_letter_queue
from fast_channels.infra.stacks.edge import EDGE_REGION

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class DemandStack(Stack):
    """What runs beside the guidance: the private zone ``<prefix>.fast.internal`` in its VPC, in ``lambda`` mode the
    bidder simulator behind an internal load balancer at ``sim.<prefix>.fast.internal``, the census gate, the daypart
    scheduled actions and the warm-up of new Prebid Server tasks.

    The stage holds it only with the CDK context ``fast-channels:demand-network``, which ``scripts/prebid_guidance.py
    deploy`` passes once the guidance is up (from ``/<prefix>/demand/*``): ``make deploy`` leaves it alone, and
    ``destroy-demand`` deletes it before the guidance.

    Args:
        config: deployment configuration (``prefix``, ``modules.demand``, log retention).
        network: the guidance's VPC, private subnets and task security group, and the simulator's code key.

    Attributes:
        vpc: the guidance's VPC.
        dns: the private zone.
        simulator: the ``lambda`` simulator, or None.
    """

    def __init__(
        self, scope: Construct, id: str, *, config: FastChannelsConfig, network: DemandNetwork, **kwargs: Any
    ) -> None:
        super().__init__(
            scope, id, description=f"FAST Channels ({config.prefix}): demand beside the guidance", **kwargs
        )
        Tags.of(self).add(MODULE_TAG, MODULE)
        demand = config.modules.demand
        self.vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "GuidanceVpc",
            vpc_id=network.vpc_id,
            availability_zones=[subnet.az for subnet in network.private_subnets],
            private_subnet_ids=[subnet.id for subnet in network.private_subnets],
            private_subnet_route_table_ids=[subnet.route_table for subnet in network.private_subnets],
        )
        self.dns = PrivateDns(self, "Dns", prefix=config.prefix, vpc=self.vpc)
        CfnOutput(self, "PrivateZoneId", value=self.dns.zone.hosted_zone_id, description="The stage's private zone")
        self.simulator: LambdaSimulator | None = None
        if demand.mode == DemandMode.DEMO and demand.simulator == Simulator.LAMBDA:
            if network.simulator_code_key is None:
                raise ValueError(f"the lambda simulator needs simulator_code_key in {network!r}")
            self.simulator = LambdaSimulator(
                self,
                "Simulator",
                prefix=config.prefix,
                vpc=self.vpc,
                task_security_group_id=network.task_security_group_id,
                code_key=network.simulator_code_key,
                zone=self.dns.zone,
                log_retention=config.observability.log_retention,
            )
            CfnOutput(
                self,
                "SimulatorFunction",
                value=self.simulator.function.function_name,
                description="The bidder simulator (lambda mode)",
            )
        self.cache_record: CacheRecord | None = None
        self.ad_server: MockAdServer | None = None
        if demand.ad_server is not AdServer.NONE:
            if network.internal_alb_dns is None or network.internal_alb_zone_id is None:
                raise ValueError(
                    f"ad_server {demand.ad_server} needs the guidance's internal load balancer in {network!r}"
                )
            self.cache_record = CacheRecord(
                self,
                "CacheRecord",
                zone=self.dns.zone,
                alb_dns=network.internal_alb_dns,
                alb_zone_id=network.internal_alb_zone_id,
            )
        if demand.ad_server is AdServer.MOCK:
            content = ssm.StringParameter.value_for_string_parameter(
                self, release_names.parameter_name(config.prefix, release_names.Parameter.CONTENT_BASE_URL)
            )
            ladder = config.ladder(config.ladders.default).version
            self.ad_server = MockAdServer(
                self,
                "AdServer",
                prefix=config.prefix,
                cache_url=f"https://{network.cloudfront_domain}{CACHE_PATH}",
                house_url=f"{content}/{specs.vast_key(ladder)}",
                impression_url=f"{content}{TRACKER_PATH}?evt=adserver",
                secret_region=EDGE_REGION,
                log_retention=config.observability.log_retention,
            )
            ssm.StringParameter(
                self,
                "AdServerUrl",
                parameter_name=parameter_path(config.prefix, Parameter.AD_SERVER_URL),
                string_value=self.ad_server.url,
                description="FAST Channels: the mock ad server's base URL (the ad-server siblings' ADS)",
            )
            CfnOutput(
                self,
                "AdServerLogGroup",
                value=self.ad_server.handler.log_group.log_group_name,
                description="The mock ad server's decisions",
            )
        if demand.ad_server is not AdServer.NONE and network.container_log_group:
            CfnOutput(
                self,
                "ContainerLogGroup",
                value=network.container_log_group,
                description="Prebid Server's container log, where the fast-ctv module logs each pod",
            )
        self.dead_letters = dead_letter_queue(self, "DeadLetters")
        dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=f"{config.prefix}-demand-dead-letters",
            description="A Census Changed or new-task event could not reach the census gate or the warm-up: Prebid "
            "Server may run without channels, or a new task may stay cold",
            topic=sns.Topic.from_topic_arn(self, "Alerts", alert_topic_arn(self, config.prefix)),
        )
        self.census_gate = CensusGate(
            self,
            "CensusGate",
            prefix=config.prefix,
            cluster_arn=network.cluster_arn,
            service_arn=network.service_arn,
            census_parameter=census_parameter_name(config.prefix),
            tasks=demand.tasks,
            log_retention=config.observability.log_retention,
            dead_letters=self.dead_letters,
        )
        self.dayparts = Dayparts(
            self,
            "Dayparts",
            prefix=config.prefix,
            cluster_arn=network.cluster_arn,
            service_arn=network.service_arn,
            dayparts=demand.dayparts,
            tasks=demand.tasks,
        )
        self.warmup = WarmUp(
            self,
            "WarmUp",
            prefix=config.prefix,
            cluster_arn=network.cluster_arn,
            service_arn=network.service_arn,
            cloudfront_domain=network.cloudfront_domain,
            target_group_arn=network.target_group_arn,
            log_retention=config.observability.log_retention,
            dead_letters=self.dead_letters,
        )
        CfnOutput(
            self,
            "CensusGateArn",
            value=self.census_gate.state_machine.state_machine_arn,
            description="Scales Prebid Server with the running channels",
        )
        CfnOutput(
            self,
            "WarmUpProjectName",
            value=self.warmup.project.project_name,
            description="Warms every new Prebid Server task",
        )
