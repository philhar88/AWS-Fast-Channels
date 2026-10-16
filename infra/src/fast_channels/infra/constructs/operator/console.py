"""Operator console hosting: the console/dist SPA in a private bucket behind its own CloudFront distribution."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, Fn, RemovalPolicy, Stack, Token
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from fast_channels.core.specs import playback_urls
from fast_channels.infra import nag
from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs.operator.nag_references import acknowledge_references, file_asset_objects

if TYPE_CHECKING:
    from fast_channels.infra.stacks.media import ChannelExports

CONSOLE_DIST: Final = REPO_ROOT / "console" / "dist"
"""Output of ``cd console && npm ci && npm run build`` (built before synth, never during it)."""
ENTRY_OBJECTS: Final = ("index.html", "config.json")
"""Objects that must never be cached for long: they point at the content-hashed ``assets/*`` and the backend."""
IMMUTABLE: Final = s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")
PLACEHOLDER: Final = (
    "<!doctype html><title>FAST Channels</title><p>Console not built: run <code>npm ci &amp;&amp; npm run build</code>"
    " in <code>console/</code> and deploy again.</p>"
)
_PREFIX_PARTS: Final = 8
"""``https://<id>.mediatailor.<r>.amazonaws.com/v1/{master|dash}/<hash>/<config>/`` split on ``/``
(ad-insertion.md §1): indexes 3 to 6 are the path."""


def content_security_policy(*, region: str, url_suffix: str, connect_sources: Sequence[str]) -> str:
    """The console's CSP (console/README.md, verified in Playwright under the production bundle).

    ``data:`` fonts are Cloudscape's embedded Open Sans; ``blob:``/``data:`` media are hls.js MSE buffers and its
    ``data:,WEBVTT`` text tracks; the hls.js transmuxer runs in a ``blob:`` worker. ``connect-src`` covers the
    media CDN, SGAI asset lists (``*.mediatailor``), the Runtime, the Gateway and Cognito, plus ``connect_sources``.
    """
    aws = f"{region}.{url_suffix}"
    connect = dict.fromkeys(
        [
            "'self'",
            *connect_sources,
            f"https://*.mediatailor.{aws}",
            f"https://bedrock-agentcore.{aws}",
            f"https://*.gateway.bedrock-agentcore.{aws}",
            f"https://cognito-idp.{aws}",
        ]
    )
    directives = {
        "default-src": "'self'",
        "script-src": "'self'",
        "style-src": "'self'",
        "img-src": "'self' data:",
        "font-src": "'self' data:",
        "media-src": "'self' blob: data:",
        "worker-src": "'self' blob:",
        "connect-src": " ".join(connect),
        "frame-ancestors": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "object-src": "'none'",
    }
    return "; ".join(f"{name} {value}" for name, value in directives.items())


def channel_playback(cdn_base_url: str, channel: ChannelExports) -> dict[str, dict[str, str]]:
    """Viewer URLs per output of an IaC channel through the CDN (``core.specs.playback_urls``).

    The playback-configuration prefixes are CloudFormation attributes; their path is cut out with ``Fn::Split`` so
    the core builder still composes the URLs.
    """
    return playback_urls(
        cdn_base=cdn_base_url,
        playback_prefix_hls=_resolvable_prefix(channel.hls_prefix),
        playback_prefix_dash=_resolvable_prefix(channel.dash_prefix),
        outputs=channel.outputs,
    )


def _resolvable_prefix(prefix: str) -> str:
    if not Token.is_unresolved(prefix):
        return prefix
    return "https://mediatailor/" + Fn.join("/", Fn.split("/", prefix, _PREFIX_PARTS)[3:7])


@dataclass(frozen=True, slots=True)
class ConsoleConfig:
    """``config.json`` of the console (CONTRACTS §H; validated by console/src/lib/config.ts)."""

    region: str
    user_pool_id: str
    user_pool_client_id: str
    cognito_domain: str
    """``https://<prefix>.auth.<region>.amazoncognito.com``, no trailing slash."""
    runtime_arn: str
    runtime_invoke_url: str
    gateway_url: str
    cdn_base_url: str
    channels: tuple[ChannelExports, ...]
    """IaC channels; the console lists agent channels through ``channels___get_channel``."""

    def document(self) -> dict[str, Any]:
        """The JSON document, with the EPG feed under the CDN's default behaviour (CONTRACTS §I)."""
        return {
            "region": self.region,
            "userPoolId": self.user_pool_id,
            "userPoolClientId": self.user_pool_client_id,
            "cognitoDomain": self.cognito_domain,
            "runtimeArn": self.runtime_arn,
            "runtimeInvokeUrl": self.runtime_invoke_url,
            "gatewayUrl": self.gateway_url,
            "cdnBaseUrl": self.cdn_base_url,
            "epgBaseUrl": f"{self.cdn_base_url}/epg",
            "channels": [
                {
                    "name": channel.name,
                    "ladderVersion": channel.ladder_version,
                    "playback": channel_playback(self.cdn_base_url, channel),
                }
                for channel in self.channels
            ],
        }


class OperatorConsole(Construct):
    """Private S3 bucket (Block Public Access, SSE-S3, TLS only) behind a CloudFront distribution with OAC, and a
    response headers policy with the console's CSP plus HSTS, ``nosniff``, ``DENY`` framing and a strict referrer.

    Content is deployed by :meth:`publish`, after the backend it points at exists. The bucket only holds build
    output, so it is always emptied and deleted with the stack.

    Args:
        cognito_domain_url: managed login domain (``https://<prefix>.auth.<region>.amazoncognito.com``).
        media_urls: media CDN and content base URLs the players fetch from.
        connect_sources: further ``connect-src`` sources, e.g. third-party ad beacon hosts with external VAST.
        dist: the console build; required whenever the stack's assets are bundled (``cdk deploy``/``synth``).
            Otherwise (``cdk destroy``, unit tests with bundling off) a placeholder page stands in for a missing
            build.

    Attributes:
        bucket, distribution, response_headers: the hosting resources.
        url: ``https://<distribution domain>/`` (the Cognito redirect URL).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        cognito_domain_url: str,
        media_urls: Sequence[str],
        connect_sources: Sequence[str] = (),
        dist: Path = CONSOLE_DIST,
    ) -> None:
        super().__init__(scope, id)
        self._dist = dist
        stack = Stack.of(self)
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
        csp = content_security_policy(
            region=stack.region,
            url_suffix=stack.url_suffix,
            connect_sources=[*media_urls, cognito_domain_url, *connect_sources],
        )
        self.response_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "Headers",
            comment="FAST Channels operator console: CSP and security headers",
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
            comment="FAST Channels operator console",
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
        nag.acknowledge(
            self.bucket,
            {nag.S3_SERVER_ACCESS_LOGS: "Holds only the public console build; reads arrive through CloudFront"},
        )
        nag.acknowledge(
            self.distribution,
            {
                "AwsSolutions-CFR1": "The console is for authenticated operators anywhere; no geo restriction",
                "AwsSolutions-CFR2": "Static SPA assets only; every API it calls authorizes Cognito tokens itself",
                "AwsSolutions-CFR3": "Static SPA assets only; operator actions are logged by the Gateway and Runtime",
                "AwsSolutions-CFR4": "The default *.cloudfront.net certificate; no custom domain for the console",
            },
        )

    def publish(self, config: ConsoleConfig) -> None:
        """Deploy the build and ``config.json`` (CONTRACTS §H), then invalidate the entry objects.

        ``assets/*`` are content-hashed and immutable; ``index.html`` and ``config.json`` are ``no-cache``. The entry
        deployment runs after the assets one, so a new ``index.html`` never references assets that are missing.
        """
        index = self._dist / "index.html"
        entry = [s3deploy.Source.json_data("config.json", config.document())]
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
                f"{index} not found: build the operator console first (cd console && npm ci && npm run build)"
            )
        else:
            assets = None
            entry.append(s3deploy.Source.data("index.html", PLACEHOLDER))
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
        acknowledge_references(
            role,
            {
                nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "CDK BucketDeployment handler: its "
                "log group is created by the stage's function defaults",
                nag.wildcard_resource(): "CDK BucketDeployment handler: cloudfront:CreateInvalidation has no "
                "resource-level permissions",
                nag.wildcard_resource(f"{self.bucket.bucket_arn}/*"): "CDK BucketDeployment handler: syncs the "
                "console build into its bucket",
                nag.wildcard_resource(file_asset_objects(stack)): "CDK BucketDeployment handler: reads the build "
                "from the CDK file-asset bucket",
                **{
                    nag.wildcard_action(action): "CDK BucketDeployment handler: S3 sync of the console build"
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
