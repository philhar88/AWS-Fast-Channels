"""The media bucket as a Channel Assembly source location."""

from aws_cdk import Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_mediatailor as mediatailor
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.infra.arns import arn


class ChannelAssemblyOrigin(Construct):
    """The media bucket as a Channel Assembly source location with ``S3_SIGV4`` access.

    Channel Assembly reads manifests from the bucket with SigV4, which needs the regional virtual-hosted BaseUrl
    (path-style, global and dual-stack hosts are rejected, channel-assembly.md §2.2) and a bucket policy that admits
    ``mediatailor.amazonaws.com`` for this source location (``MediaStorage.allow_channel_assembly_read``).
    Segment URLs in channel manifests are absolute and taken from ``DefaultSegmentDeliveryConfiguration``; SSAI does
    not rewrite them (channel-assembly.md §6.3), so the content CDN host is chosen here.

    Args:
        name: source location name (``core.naming.source_location_name``); the bucket policy admits exactly it.
        bucket: the media bucket (SSE-S3: SSE-KMS breaks manifest reads, channel-assembly.md §2.4).
        content_base_url: absolute base URL of content segments (``delivery.content_domain`` or CloudFront).

    Attributes:
        source_location: the ``CfnSourceLocation``.
        name: its name. arn: its ARN. base_url: its S3 BaseUrl.
    """

    def __init__(self, scope: Construct, id: str, *, name: str, bucket: s3.IBucket, content_base_url: str) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.name = name
        self.base_url = f"https://{bucket.bucket_name}.s3.{stack.region}.{stack.url_suffix}"
        self._bucket = bucket
        self.source_location = mediatailor.CfnSourceLocation(
            self,
            "Resource",
            source_location_name=name,
            http_configuration=mediatailor.CfnSourceLocation.HttpConfigurationProperty(base_url=self.base_url),
            access_configuration=mediatailor.CfnSourceLocation.AccessConfigurationProperty(access_type="S3_SIGV4"),
            default_segment_delivery_configuration=(
                mediatailor.CfnSourceLocation.DefaultSegmentDeliveryConfigurationProperty(base_url=content_base_url)
            ),
        )
        self.arn = self.source_location.attr_arn

    def grant_vod_sources(self, grantee: iam.IGrantable) -> iam.Grant:
        """Let ``grantee`` register and maintain VOD sources here (the ingest pipeline, the origins tools).

        CreateVodSource also checks that the *caller* can read the manifests (channel-assembly.md §2.3), hence
        ``s3:GetObject`` on the asset prefix. The grantee's own construct acknowledges the resulting wildcards.
        """
        self._bucket.grant_read(grantee, "assets/*")
        return iam.Grant.add_to_principal(
            grantee=grantee,
            actions=[
                "mediatailor:CreateVodSource",
                "mediatailor:DescribeVodSource",
                "mediatailor:UpdateVodSource",
                "mediatailor:DeleteVodSource",
                "mediatailor:TagResource",
            ],
            resource_arns=[arn(self, "mediatailor", "vodSource", f"{self.name}/*")],
        )
