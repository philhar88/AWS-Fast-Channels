"""``MockAdServer`` and ``CacheRecord``: the ad-server mode's pieces beside the Prebid Server guidance (M8-09).

- ``MockAdServer`` (``modules.demand.ad_server: mock``): the demo's GAM-like ad server
  (``fast_channels.functions.demand.ad_server``), the ad-server siblings' ADS. MediaTailor calls it from the internet,
  so it sits behind an API Gateway HTTP API whose only route is ``GET /ads``; the handler answers only requests that
  carry the stage's origin secret, as the demand origin's rule requires for Prebid Server, and the API throttles low.
  The function's permission is scoped to the API (an account guardrail removes Lambda permissions open to anyone).
- ``CacheRecord``: ``cache.<prefix>.fast.internal``, an alias of the guidance's internal load balancer, which takes
  Prebid Cache's writes (``POST /cache``) from the Prebid Server tasks only. The rendered configuration's
  ``cache.internal.host`` names it, so writes never leave the VPC.
"""

from typing import Final

import jsii
from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigateway
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from constructs import Construct

from fast_channels.core.demand import ORIGIN_SECRET_SLOTS, origin_secret
from fast_channels.core.pbs_config import CACHE_RECORD
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction

THROTTLE_RATE: Final = 200
THROTTLE_BURST: Final = 400
"""Requests per second the mock's API takes: one ad request per break and session, at demo scale."""
WHY: Final = (
    "The demo's mock ad server: MediaTailor's ADS for the ad-server siblings, which must reach it from AWS's edge"
)


def ad_server_function(prefix: str) -> str:
    return f"{prefix}-demand-ad-server"


class MockAdServer(Construct):
    """The mock ad server's function and HTTP API.

    Args:
        prefix: the stage's prefix.
        cache_url: Prebid Cache's public endpoint (``https://<guidance CloudFront>/cache``).
        house_url: the stage's house pod (the house line's Wrapper target).
        impression_url: the ad server's own impression (our CDN's beacon, without a decision: delivery ignores it).
        secret_region: the region of the origin secrets (the edge's).
        log_retention: the function's log retention.

    Attributes:
        url: the API's base URL (the ad-server siblings' ADS URL starts with it).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        cache_url: str,
        house_url: str,
        impression_url: str,
        secret_region: str,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        secrets = [origin_secret(prefix, slot) for slot in ORIGIN_SECRET_SLOTS]
        self.handler = FastFunction(
            self,
            "Function",
            handler="fast_channels.functions.demand.ad_server.handler",
            description="FAST Channels: the demo's mock ad server (Prebid line items, a Wrapper to the cached pod)",
            environment={
                "CACHE_URL": cache_url,
                "HOUSE_URL": house_url,
                "IMPRESSION_URL": impression_url,
                "ORIGIN_SECRETS": ",".join(secrets),
                "ORIGIN_SECRET_REGION": secret_region,
            },
            timeout=Duration.seconds(5),
            memory_size=256,
            log_retention=log_retention,
        )
        self.handler.allow(
            ["secretsmanager:GetSecretValue"],
            [
                f"arn:{stack.partition}:secretsmanager:{secret_region}:{stack.account}:secret:{name}-*"
                for name in secrets
            ],
            why="The two origin secrets (Secrets Manager adds a random suffix to a secret's ARN)",
        )
        nag.acknowledge(self.handler.function, {nag.LAMBDA_DLQ: f"{WHY}; invoked synchronously by API Gateway"})
        self.api = apigateway.HttpApi(
            self,
            "Api",
            api_name=ad_server_function(prefix),
            description="FAST Channels: the demo's mock ad server (GET /ads, origin secret required)",
            create_default_stage=False,
        )
        self.api.add_routes(
            path="/ads",
            methods=[apigateway.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("Ads", self.handler.function),
        )
        stage = apigateway.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigateway.ThrottleSettings(rate_limit=THROTTLE_RATE, burst_limit=THROTTLE_BURST),
        )
        nag.acknowledge(
            stage,
            {
                "AwsSolutions-APIG1": f"{WHY}; the function logs every answer with its line, session and break",
            },
        )
        nag.acknowledge(
            self.api,
            {
                "AwsSolutions-APIG4": f"{WHY}; the handler requires the stage's origin secret, as the demand origin's "
                "rule does for Prebid Server",
            },
        )
        self.url = self.api.api_endpoint
        CfnOutput(self, "Url", value=self.url, description="The mock ad server's base URL (GET /ads)")


@jsii.implements(route53.IAliasRecordTarget)
class _BalancerAlias:
    """An alias to a load balancer known only by its DNS name and zone (another app's: the guidance's)."""

    def __init__(self, dns_name: str, zone_id: str) -> None:
        self.dns_name, self.zone_id = dns_name, zone_id

    def bind(
        self, _record: route53.IRecordSet, _zone: route53.IHostedZone | None = None
    ) -> route53.AliasRecordTargetConfig:
        # What route53_targets.LoadBalancerTarget gives an application load balancer.
        return route53.AliasRecordTargetConfig(dns_name=f"dualstack.{self.dns_name}", hosted_zone_id=self.zone_id)


class CacheRecord(Construct):
    """``cache.<prefix>.fast.internal`` -> the guidance's internal load balancer (Prebid Cache's write path)."""

    def __init__(self, scope: Construct, id: str, *, zone: route53.IHostedZone, alb_dns: str, alb_zone_id: str) -> None:
        super().__init__(scope, id)
        self.record = route53.ARecord(
            self,
            "Record",
            zone=zone,
            record_name=CACHE_RECORD,
            target=route53.RecordTarget.from_alias(_BalancerAlias(alb_dns, alb_zone_id)),
        )
