"""Stateful storage of a deployment: the ingest, media and logs buckets and the two DynamoDB tables (SPEC §4).

The tables' index name ``GSI1`` and TTL attribute come from the MediaLibrary schema module
(``fast_channels.functions.media_library``) that the functions read and write the items with.
"""

from collections.abc import Sequence
from typing import Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.functions.media_library import GSI1, TTL_ATTRIBUTE
from fast_channels.infra import nag
from fast_channels.infra.arns import arn

__all__ = [
    "ASSETS_PREFIX",
    "DEFAULT_LOG_EXPIRATION",
    "GSI1",
    "TTL_ATTRIBUTE",
    "MediaLibrary",
    "MediaStorage",
    "ReferenceCatalogTable",
]
ASSETS_PREFIX: Final = "assets/"
"""Encoder outputs ``assets/<asset_id>/<ladder_version>/``; their manifests are for Channel Assembly only."""

DEFAULT_LOG_EXPIRATION: Final = Duration.days(90)
_ABORT_UPLOADS: Final = s3.LifecycleRule(
    id="abort-incomplete-uploads", abort_incomplete_multipart_upload_after=Duration.days(7)
)


class MediaStorage(Construct):
    """The deployment's three buckets, each SSE-S3, bucket-owner enforced, private and TLS 1.2+ only.

    - ``ingest_bucket``: mezzanines (and optional ``<key>.json`` sidecars). EventBridge notifications are on;
      objects move to Intelligent-Tiering.
    - ``media_bucket``: encoder outputs ``assets/<asset>/<ladder>/``, preconditioned ads ``ads/<ladder>/`` and
      EPG feeds ``epg/<channel>.{json,xml}``. Channel Assembly reads its manifests with SigV4, CloudFront reads
      everything else through OAC; SSE-KMS would break Channel Assembly (channel-assembly.md §2.4).
    - ``logs_bucket``: S3 server access logs (``s3/<bucket>/``) and CloudFront standard logs v2.

    Grants to AWS services go through the ``allow_*`` methods. They use synth-time ARN patterns instead of tokens
    from consumer stacks, so each bucket keeps a single bucket policy owned by the stack that owns the bucket and
    no cross-stack cycle can form (SPEC §2).

    Args:
        removal_policy: ``DESTROY`` also empties the buckets on deletion.
        cors_origins: origins browser players may fetch media from (via CloudFront).
        log_expiration: how long access and CDN logs are kept.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        cors_origins: Sequence[str] = ("*",),
        log_expiration: Duration = DEFAULT_LOG_EXPIRATION,
    ) -> None:
        super().__init__(scope, id)
        self._removal_policy = removal_policy
        self.logs_bucket = self._bucket(
            "Logs", lifecycle_rules=[_ABORT_UPLOADS, s3.LifecycleRule(id="expire-logs", expiration=log_expiration)]
        )
        nag.acknowledge(self.logs_bucket, {nag.S3_SERVER_ACCESS_LOGS: "This bucket receives the access logs"})
        self.ingest_bucket = self._bucket(
            "Ingest",
            access_logs_prefix="s3/ingest/",
            lifecycle_rules=[
                _ABORT_UPLOADS,
                s3.LifecycleRule(
                    id="mezzanines-intelligent-tiering",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.INTELLIGENT_TIERING, transition_after=Duration.days(0)
                        )
                    ],
                ),
            ],
        )
        # Native EventBridge notifications; the L2 flag would add a custom-resource Lambda (cdk-standards.md §7.1).
        self.ingest_bucket.node.default_child.add_property_override(  # type: ignore[union-attr]
            "NotificationConfiguration.EventBridgeConfiguration.EventBridgeEnabled", True
        )
        self.media_bucket = self._bucket(
            "Media",
            access_logs_prefix="s3/media/",
            lifecycle_rules=[_ABORT_UPLOADS],
            # Preflight needs this rule and the CORS-S3Origin request policy on the CDN behaviour (cdn.md §4.1).
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET, s3.HttpMethods.HEAD],
                    allowed_origins=list(cors_origins),
                    allowed_headers=["*"],
                    max_age=86400,
                )
            ],
        )

    def _bucket(self, id: str, *, access_logs_prefix: str | None = None, **props: Any) -> s3.Bucket:
        return s3.Bucket(
            self,
            id,
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=self._removal_policy,
            auto_delete_objects=self._removal_policy == RemovalPolicy.DESTROY,
            server_access_logs_bucket=self.logs_bucket if access_logs_prefix else None,
            server_access_logs_prefix=access_logs_prefix,
            target_object_key_format=s3.TargetObjectKeyFormat.partitioned_prefix(s3.PartitionDateSource.EVENT_TIME)
            if access_logs_prefix
            else None,
            **props,
        )

    def allow_cloudfront_read(self) -> None:
        """Let CloudFront distributions of this account read the media bucket through OAC, except content
        manifests: ``assets/*.m3u8`` and ``assets/*.mpd`` are only for Channel Assembly.

        The deny stops casual direct playback of an asset from the CDN; it does not make ads mandatory. Content
        segments stay public at predictable names (``assets/<asset>/<ladder>/main_<rendition>_<n>.cmfv``, and every
        SSAI playlist shows the prefix), so anyone can rebuild an ad-free playlist. Where that matters, protect
        ``/assets/*`` with CloudFront signed cookies or URLs, or a token check at the edge.
        """
        stack = Stack.of(self)
        cloudfront = iam.ServicePrincipal("cloudfront.amazonaws.com")
        self.media_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudFrontRead",
                principals=[cloudfront],
                actions=["s3:GetObject"],
                resources=[self.media_bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {"AWS:SourceAccount": stack.account},
                    "ArnLike": {"AWS:SourceArn": f"arn:{stack.partition}:cloudfront::{stack.account}:distribution/*"},
                },
            )
        )
        self.media_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudFrontDenyContentManifests",
                effect=iam.Effect.DENY,
                principals=[cloudfront],
                actions=["s3:GetObject"],
                resources=[
                    self.media_bucket.arn_for_objects(f"{ASSETS_PREFIX}*.m3u8"),
                    self.media_bucket.arn_for_objects(f"{ASSETS_PREFIX}*.mpd"),
                ],
            )
        )

    def allow_channel_assembly_read(self, source_location: str) -> None:
        """Let the Channel Assembly source location named ``source_location`` read assets (S3_SIGV4 access).

        Exactly that one: the agent's own source locations never point at this bucket (``create_source_location``
        refuses it), and a name pattern would also admit other deployments' locations (``fast*`` matches
        ``fastci-media``). MediaTailor presents the source location ARN as ``aws:SourceArn`` (verified,
        channel-assembly.md §2.3); the name is a synth-time string, so the policy references no other stack.
        """
        stack = Stack.of(self)
        self.media_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="ChannelAssemblyRead",
                principals=[iam.ServicePrincipal("mediatailor.amazonaws.com")],
                actions=["s3:GetObject"],
                resources=[self.media_bucket.arn_for_objects(f"{ASSETS_PREFIX}*")],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {"aws:SourceArn": arn(self, "mediatailor", "sourceLocation", source_location)},
                },
            )
        )

    def allow_log_delivery(self) -> None:
        """Let CloudWatch Logs vended-log deliveries of this account write to the logs bucket (CloudFront standard
        logs v2, whose delivery sources always live in us-east-1).

        Declaring the statements here keeps CreateDelivery from adding them out of band, where the next
        deployment of this stack would remove them again.
        """
        stack = Stack.of(self)
        delivery = iam.ServicePrincipal("delivery.logs.amazonaws.com")
        sources = {"ArnLike": {"aws:SourceArn": f"arn:{stack.partition}:logs:us-east-1:{stack.account}:*"}}
        self.logs_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LogDeliveryAclCheck",
                principals=[delivery],
                actions=["s3:GetBucketAcl"],
                resources=[self.logs_bucket.bucket_arn],
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}, **sources},
            )
        )
        self.logs_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="LogDeliveryWrite",
                principals=[delivery],
                actions=["s3:PutObject"],
                resources=[self.logs_bucket.arn_for_objects("*")],
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account, "s3:x-amz-acl": "bucket-owner-full-control"},
                    **sources,
                },
            )
        )


def _single_table(**props: Any) -> dict[str, Any]:
    return {
        "partition_key": dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
        "sort_key": dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
        "billing": dynamodb.Billing.on_demand(),
        "point_in_time_recovery_specification": dynamodb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True
        ),
        "global_secondary_indexes": [
            dynamodb.GlobalSecondaryIndexPropsV2(
                index_name=GSI1,
                partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
            )
        ],
        **props,
    }


class MediaLibrary(dynamodb.TableV2):
    """Single-table media library (CONTRACTS §D): assets, ladders and proposals, channels with their EPG, plans
    and leases. Keys ``pk``/``sk``, index ``GSI1`` (``gsi1pk``/``gsi1sk``), TTL ``expires_at``, on demand, PITR,
    AWS-owned encryption key."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        deletion_protection: bool = False,
    ) -> None:
        super().__init__(
            scope,
            id,
            **_single_table(
                time_to_live_attribute=TTL_ATTRIBUTE,
                removal_policy=removal_policy,
                deletion_protection=deletion_protection,
            ),
        )


class ReferenceCatalogTable(dynamodb.TableV2):
    """Reference content catalog (the BYO-CMS contract's sample implementation): ``CONTENT#<id>``/``META`` items,
    one ``GSI1`` entry ``GENRE#<genre>``/``<title>`` per item, on demand, PITR."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        deletion_protection: bool = False,
    ) -> None:
        super().__init__(
            scope, id, **_single_table(removal_policy=removal_policy, deletion_protection=deletion_protection)
        )
