"""``FastChannels-Identity``: viewer identity (``modules.identity``; ADR-0018, DESIGN-v2.2 §5)."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm

from fast_channels.core.identity.names import (
    PLAYBACK_SCOPE,
    Parameter,
    parameter_name,
    table_name,
    vid_key_alias,
)
from fast_channels.core.monetization import names as release_names
from fast_channels.core.monetization.names import plan_key
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.identity.api import IdentityApi
from fast_channels.infra.constructs.identity.viewers import ViewerPool
from fast_channels.infra.exports import IdentityExports

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class IdentityStack(Stack):
    """The viewer pool, the ticket table, the ``vid`` key and the identity API, deployed when
    ``modules.identity.enabled``.

    - **Viewer pool** (:class:`~fast_channels.infra.constructs.identity.viewers.ViewerPool`): Cognito Essentials, the
      scope ``fast/playback`` in every access token.
    - **Ticket table** ``<prefix>-identity``: one row ``T#<t>`` per minted ticket (``core.identity.ticket``), deleted by
      DynamoDB TTL a day after the longest binding lease.
    - **``vid`` key** ``alias/<prefix>-identity-vid``: a KMS HMAC-SHA-256 key. ``vid = HMAC(iss|sub)`` truncated to 128
      bits (``core.identity.vid``); the key never leaves KMS, so nobody can compute the ``vid`` of a ``sub`` without
      ``kms:GenerateMac`` on it. It is kept with the stack's removal policy: a new key changes every ``vid``.
    - **Mint** (``functions.identity.mint``): reads the channel's playback plan from the media bucket, puts the row and
      answers the CDN URL with ``?ads.t=<t>``.
    - **Identity API** (:class:`~fast_channels.infra.constructs.identity.api.IdentityApi`): ``POST /playback`` and
      ``POST /redeem``.

    Other stacks and the scripts read the stack's values from the SSM parameters ``/<prefix>/identity/<name>``
    (``core.identity.names.Parameter``).

    Args:
        config: deployment configuration (``modules.identity``, ``modules.release.channels``, ``observability``,
            ``removal``).

    The media bucket, where the playback plans are, is read from the release module's SSM parameter
    ``/<prefix>/release/media-bucket`` (the Media stack writes it), so no other stack owes this one an output.

    Attributes:
        exports: what the viewer app's stack consumes.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): viewer identity", **kwargs)
        settings = config.modules.identity
        media_bucket = s3.Bucket.from_bucket_name(
            self,
            "MediaBucket",
            ssm.StringParameter.value_for_string_parameter(
                self, release_names.parameter_name(config.prefix, release_names.Parameter.MEDIA_BUCKET)
            ),
        )
        retention = config.observability.log_retention
        removal = config.removal_policy
        self.table = dynamodb.TableV2(
            self,
            "Tickets",
            table_name=table_name(config.prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="ttl",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=removal,
        )
        self.vid_key = kms.Key(
            self,
            "VidKey",
            alias=vid_key_alias(config.prefix),
            description=f"FAST Channels ({config.prefix}): the viewer id, HMAC(iss|sub)",
            key_spec=kms.KeySpec.HMAC_256,
            key_usage=kms.KeyUsage.GENERATE_VERIFY_MAC,
            removal_policy=removal,
            pending_window=Duration.days(7),
        )
        nag.acknowledge(
            self.vid_key, {"AwsSolutions-KMS5": "KMS does not rotate HMAC keys; a new key changes every vid"}
        )
        self.viewers = ViewerPool(
            self, "Viewers", prefix=config.prefix, log_retention=retention, removal_policy=removal
        )
        self.mint = FastFunction(
            self,
            "Mint",
            handler="fast_channels.functions.identity.mint.handler",
            description="POST /playback: a signed-in viewer's single-use playback ticket",
            environment={
                "FAST_PREFIX": config.prefix,
                "MEDIA_BUCKET": media_bucket.bucket_name,
                "IDENTITY_TABLE": self.table.table_name,
                "VID_KEY": self.vid_key.key_arn,
                "RELEASE_CHANNELS": ",".join(config.modules.release.channels),  # the monetized ones: not the lab
                "REDEEM_WINDOW_MS": str(settings.redeem_window_seconds * 1000),
            },
            timeout=Duration.seconds(10),
            memory_size=512,
            log_retention=retention,
            removal_policy=removal,
            compiled=True,
        )
        nag.acknowledge(self.mint, {nag.LAMBDA_DLQ: "Invoked synchronously by API Gateway; the player retries"})
        self.mint.allow(
            ["s3:GetObject"],
            [media_bucket.arn_for_objects(plan_key("*"))],
            why="the channels' playback plans (plan/playback/<channel>.json)",
        )
        self.mint.allow(["dynamodb:PutItem"], [self.table.table_arn])
        self.mint.allow(["kms:GenerateMac"], [self.vid_key.key_arn])
        self.api = IdentityApi(
            self,
            "Api",
            prefix=config.prefix,
            user_pool=self.viewers.user_pool,
            mint=self.mint.function,
            table=self.table,
            rate_limit=settings.api_rate_limit,
            burst_limit=settings.api_burst_limit,
            log_retention=retention,
            removal_policy=removal,
        )
        issuer = f"https://cognito-idp.{self.region}.{self.url_suffix}/{self.viewers.user_pool.user_pool_id}"
        client_id = self.viewers.client.user_pool_client_id
        values = {
            Parameter.USER_POOL_ID: self.viewers.user_pool.user_pool_id,
            Parameter.CLIENT_ID: client_id,
            Parameter.E2E_CLIENT_ID: client_id,
            Parameter.ISSUER: issuer,
            Parameter.VID_KEY: self.vid_key.key_arn,
            Parameter.TABLE: self.table.table_name,
            Parameter.PLAYBACK_URL: self.api.playback_url,
            Parameter.REDEEM_URL: self.api.redeem_url,
        }
        for parameter, value in values.items():
            ssm.StringParameter(
                self,
                f"Parameter{''.join(part.title() for part in parameter.value.split('-'))}",
                parameter_name=parameter_name(config.prefix, parameter),
                string_value=value,
                description=f"Identity module: {parameter.value}",
            )
        outputs = {
            "UserPoolId": (self.viewers.user_pool.user_pool_id, "The viewer pool"),
            "UserPoolClientId": (client_id, "The viewer app client (public: SRP and password sign-in)"),
            "PlaybackApiUrl": (self.api.api.url, "POST <url>playback mints a playback ticket"),
            "RedeemUrl": (self.api.redeem_url, "The PRE_SESSION Function's redeem"),
            "IdentityTable": (self.table.table_name, "The ticket table (T#<t>)"),
            "Region": (self.region, "Where the pool and the API are"),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
        self.exports = IdentityExports(
            user_pool=self.viewers.user_pool,
            user_pool_id=self.viewers.user_pool.user_pool_id,
            user_pool_client_id=client_id,
            resource_server=self.viewers.resource_server,
            playback_scope=PLAYBACK_SCOPE,
            playback_api_url=self.api.api.url,
            region=self.region,
        )
