"""``PrivateDns``: the stage's private zone ``<prefix>.fast.internal``, in the guidance's VPC.

Prebid Server's configuration names its in-VPC endpoints in this zone before they exist (``core/pbs_config.py``): the
Pod Decision Service at ``pds.<prefix>.fast.internal`` and, in ``lambda`` mode, the bidder simulator at
``sim.<prefix>.fast.internal``. Until a name has a record, a call to it fails like an unreachable endpoint, which the
fast-ctv module's no-hook contract covers.
"""

from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_route53 as route53
from constructs import Construct

from fast_channels.core.pbs_config import private_zone


class PrivateDns(Construct):
    """The private hosted zone.

    Attributes:
        zone: ``<prefix>.fast.internal``, associated with the guidance's VPC.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str, vpc: ec2.IVpc) -> None:
        super().__init__(scope, id)
        self.zone = route53.PrivateHostedZone(
            self,
            "Zone",
            zone_name=private_zone(prefix),
            vpc=vpc,
            comment=f"FAST Channels ({prefix}): Prebid Server's in-VPC endpoints",
        )
