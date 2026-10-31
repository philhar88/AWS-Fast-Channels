"""``FastChannels-<prefix>-DemandEdge``: the demand origin's gate, in us-east-1 (CloudFront's WAF scope)."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack, Tags

from fast_channels.core.ad_server import AdServer
from fast_channels.core.demand import MODULE, MODULE_TAG
from fast_channels.infra.constructs.demand.origin_waf import OriginGate

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class DemandEdgeStack(Stack):
    """The two origin secrets and the WAF rule group the guidance's web ACL references first (DESIGN §6.10).

    Deployed before the guidance, which reads ``OriginRuleGroupArn`` once, when it creates its web ACL; the rule
    group therefore lives as long as the guidance does. The release module puts origin secret A into the monetized
    playback configurations' header templates and rotates the two values.

    Args:
        config: deployment configuration (``prefix``, ``removal``, ``modules.demand.rate_limit_per_ip``).

    Attributes:
        gate: the secrets and the rule group.
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): demand origin gate", **kwargs)
        Tags.of(self).add(MODULE_TAG, MODULE)
        self.gate = OriginGate(
            self,
            "OriginGate",
            prefix=config.prefix,
            rate_limit_per_ip=config.modules.demand.rate_limit_per_ip,
            removal_policy=config.removal_policy,
            cache_reads=config.modules.demand.ad_server is not AdServer.NONE,
        )
        CfnOutput(
            self,
            "OriginRuleGroupArn",
            value=self.gate.rule_group.attr_arn,
            description="WAF rule group the guidance's web ACL references first (ExtraWafRuleGroupArn)",
        )
        for slot, secret in zip(("A", "B"), self.gate.secrets, strict=True):
            CfnOutput(self, f"OriginSecret{slot}Arn", value=secret.secret_arn, description=f"Origin secret {slot}")
