"""The catalog-context bucket: where the context publisher writes ``catalog/ctx/v1/<content_id>.json``.

It is a bucket of its own, not the media bucket, because a miss must be a **404**. MediaTailor's Function cache
stores a 404 but not a 403, and S3 answers 403 for a missing key unless the requester may list the bucket. The media
bucket's policy, which belongs to the Data stack and to v3.0.0, stays as it is; this bucket's policy lets the media
distribution list it. That is safe because the distribution's route to it (``/catalog/ctx/*``, no query string in
its cache key) can only ever ask for an object under ``catalog/ctx/``, never for the bucket root or a listing.
"""

from typing import Final

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.infra import nag

OBJECTS: Final = "catalog/ctx/*"
"""Every key the distribution may read: the context objects of this and any later schema version."""


class CatalogContextBucket(Construct):
    """A private bucket (SSE-S3, bucket owner enforced, TLS 1.2+ only) holding the catalog context objects.

    Pass :attr:`reference` to the media distribution as its catalog origin, then :meth:`admit` that distribution:
    the bucket policy grants it ``s3:GetObject`` on ``catalog/ctx/*`` and ``s3:ListBucket`` on the bucket, both only
    for requests whose ``AWS:SourceArn`` is that distribution.

    Args:
        removal_policy: ``DESTROY`` also empties the bucket on deletion (its objects are rebuilt from the catalog).

    Attributes:
        bucket: the bucket; reference: an imported view of it, so that the CloudFront origin adds no policy statement
        of its own.
    """

    def __init__(self, scope: Construct, id: str, *, removal_policy: RemovalPolicy = RemovalPolicy.DESTROY) -> None:
        super().__init__(scope, id)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=removal_policy,
            auto_delete_objects=removal_policy == RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="abort-incomplete-uploads", abort_incomplete_multipart_upload_after=Duration.days(7)
                )
            ],
        )
        nag.acknowledge(
            self.bucket,
            {
                nag.S3_SERVER_ACCESS_LOGS: "Every read goes through the media distribution, whose standard logs record "
                "it; the only writer is the context publisher, whose log records each object",
            },
        )
        self.reference = s3.Bucket.from_bucket_attributes(
            self,
            "Reference",
            bucket_arn=self.bucket.bucket_arn,
            bucket_name=self.bucket.bucket_name,
            bucket_regional_domain_name=self.bucket.bucket_regional_domain_name,
        )

    def admit(self, distribution: cf.IDistribution) -> None:
        """Let ``distribution`` read the context objects and list the bucket (its misses become 404s)."""
        cloudfront = iam.ServicePrincipal("cloudfront.amazonaws.com")
        source = {"StringEquals": {"AWS:SourceArn": distribution.distribution_arn}}
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudFrontReadContexts",
                principals=[cloudfront],
                actions=["s3:GetObject"],
                resources=[self.bucket.arn_for_objects(OBJECTS)],
                conditions=source,
            )
        )
        self.bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="CloudFrontListForMisses",
                principals=[cloudfront],
                actions=["s3:ListBucket"],
                resources=[self.bucket.bucket_arn],
                conditions=source,
            )
        )
