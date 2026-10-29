"""``FastChannels-<prefix>-Demand``: what runs beside the Prebid Server guidance, in its VPC."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack, Tags
from aws_cdk import aws_ec2 as ec2

from fast_channels.core.demand import MODULE, MODULE_TAG, DemandNetwork
from fast_channels.infra.config import DemandMode, Simulator
from fast_channels.infra.constructs.demand.private_dns import PrivateDns
from fast_channels.infra.constructs.demand.simulator import LambdaSimulator

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class DemandStack(Stack):
    """The private zone ``<prefix>.fast.internal`` in the guidance's VPC, and in ``lambda`` mode the bidder simulator
    behind an internal load balancer at ``sim.<prefix>.fast.internal``.

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
