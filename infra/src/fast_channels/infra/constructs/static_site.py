"""A single-page app in a private bucket behind its own CloudFront distribution: the operator console's hosting,
shared with the viewer module's reference viewer (each on its own origin, so their tokens never share one)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.nag import acknowledge_references, file_asset_objects

__all__ = ["ENTRY_OBJECTS", "IMMUTABLE", "SiteText", "StaticSite", "content_security_policy"]

ENTRY_OBJECTS: Final = ("index.html", "config.json")
"""Objects that must never be cached for long: they point at the content-hashed ``assets/*`` and the backend."""
IMMUTABLE: Final = s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")


def content_security_policy(connect_sources: Sequence[str]) -> str:
    """A site's CSP (verified in Playwright under the production bundle): no inline script or style.

    ``data:`` fonts are Cloudscape's embedded Open Sans; ``blob:`` media are hls.js MSE buffers, and ``data:`` media
    the hidden ``metadata`` track (``src="data:,WEBVTT"``) that hls.js creates for EXT-X-DATERANGE tags: SSAI
    playlists still carry Channel Assembly's DATERANGE ad markers. The hls.js transmuxer runs in a ``blob:`` worker.
    ``connect-src`` is ``'self'`` plus ``connect_sources``, each once.
    """
    directives = {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' data:",
        "font-src": "'self' data:",
        "media-src": "'self' blob: data:",
        "worker-src": "'self' blob:",
        "connect-src": " ".join(dict.fromkeys(["'self'", *connect_sources])),
        "frame-ancestors": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "object-src": "'none'",
    }
    return "; ".join(f"{name} {value}" for name, value in directives.items())


@dataclass(frozen=True, slots=True)
class SiteText:
    """What a site says about itself: resource comments, cdk-nag reasons and the build it needs."""

    name: str
    """``operator console``: the comments' ``FAST Channels <name>``."""
    build: str
    """``console``: the deployment handler's reasons say ``the <build> build``."""
    build_command: str
    """How to build ``dist``, for the missing-build error."""
    placeholder: str
    """The page that stands in for a missing build where bundling is off (``cdk destroy``, unit tests)."""
    bucket: str
    """Why the bucket has no server access logs."""
    distribution: Mapping[str, str]
    """cdk-nag reasons of the distribution (geo restriction, WAF, access logs, certificate)."""


class StaticSite(Construct):
    """Private S3 bucket (Block Public Access, SSE-S3, TLS only) behind a CloudFront distribution with OAC, and a
    response headers policy with the site's CSP plus HSTS, ``nosniff``, ``DENY`` framing and a strict referrer.

    Content is deployed by :meth:`publish_document`, after the backend it points at exists. The bucket only holds
    build output, so it is always emptied and deleted with the stack.

    Args:
        csp: the site's Content-Security-Policy (:func:`content_security_policy`).
        dist: the build; required whenever the stack's assets are bundled (``cdk deploy``/``synth``). Otherwise a
            placeholder page stands in for a missing build.
        text: the comments, reasons and messages that name the site.

    Attributes:
        bucket, distribution, response_headers: the hosting resources.
        url: ``https://<distribution domain>/``.
    """

    def __init__(self, scope: Construct, id: str, *, csp: str, dist: Path, text: SiteText) -> None:
        super().__init__(scope, id)
        self._dist = dist
        self._text = text
        self.bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.response_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "Headers",
            comment=f"FAST Channels {text.name}: CSP and security headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=csp, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override=True
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True
                ),
            ),
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"FAST Channels {text.name}",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=self.response_headers,
            ),
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        self.url = f"https://{self.distribution.distribution_domain_name}/"
        nag.acknowledge(self.bucket, {nag.S3_SERVER_ACCESS_LOGS: text.bucket})
        nag.acknowledge(self.distribution, dict(text.distribution))

    def publish_document(self, document: Mapping[str, Any]) -> None:
        """Deploy the build and ``config.json`` (``document``), then invalidate the entry objects.

        ``assets/*`` are content-hashed and immutable; ``index.html`` and ``config.json`` are ``no-cache``. The entry
        deployment runs after the assets one, so a new ``index.html`` never references assets that are missing.
        """
        index = self._dist / "index.html"
        entry = [s3deploy.Source.json_data("config.json", document)]
        if index.is_file():
            assets = s3deploy.BucketDeployment(
                self,
                "Assets",
                destination_bucket=self.bucket,
                sources=[s3deploy.Source.asset(str(self._dist))],
                exclude=list(ENTRY_OBJECTS),
                cache_control=[IMMUTABLE],
                prune=True,
            )
            entry.append(s3deploy.Source.data("index.html", index.read_text()))
        elif Stack.of(self).bundling_required:
            raise FileNotFoundError(
                f"{index} not found: build the {self._text.name} first ({self._text.build_command})"
            )
        else:
            assets = None
            entry.append(s3deploy.Source.data("index.html", self._text.placeholder))
        deployment = s3deploy.BucketDeployment(
            self,
            "Entry",
            destination_bucket=self.bucket,
            sources=entry,
            cache_control=[s3deploy.CacheControl.no_cache()],
            prune=False,
            distribution=self.distribution,
            distribution_paths=["/", *(f"/{name}" for name in ENTRY_OBJECTS)],
        )
        if assets is not None:
            deployment.node.add_dependency(assets)
        self._acknowledge_deployment_handler(deployment)

    def _acknowledge_deployment_handler(self, deployment: s3deploy.BucketDeployment) -> None:
        """The BucketDeployment singleton handler (CDK's): its findings, with reasons."""
        stack = Stack.of(self)
        role = deployment.handler_role
        build = self._text.build
        acknowledge_references(
            role,
            {
                nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "CDK BucketDeployment handler: its "
                "log group is created by the stage's function defaults",
                nag.wildcard_resource(): "CDK BucketDeployment handler: cloudfront:CreateInvalidation has no "
                "resource-level permissions",
                nag.wildcard_resource(f"{self.bucket.bucket_arn}/*"): "CDK BucketDeployment handler: syncs the "
                f"{build} build into its bucket",
                nag.wildcard_resource(file_asset_objects(stack)): "CDK BucketDeployment handler: reads the build "
                "from the CDK file-asset bucket",
                **{
                    nag.wildcard_action(action): f"CDK BucketDeployment handler: S3 sync of the {build} build"
                    for action in ("s3:GetObject*", "s3:GetBucket*", "s3:List*", "s3:DeleteObject*", "s3:Abort*")
                },
            },
        )
        nag.acknowledge(
            role.node.scope,  # type: ignore[arg-type]
            {
                "AwsSolutions-L1": "CDK BucketDeployment handler: its runtime is chosen by aws-cdk-lib",
                "Serverless-LambdaLatestVersion": "CDK BucketDeployment handler: its runtime is chosen by aws-cdk-lib",
                nag.LAMBDA_DLQ: "CloudFormation custom resource handler: CloudFormation reports its failures",
            },
        )
