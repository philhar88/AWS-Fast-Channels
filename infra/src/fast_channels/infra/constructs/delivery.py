"""Media delivery (SPEC §5.4): one CloudFront distribution in front of the media bucket and MediaTailor, its
CDN-neutral path contract, and its standard-logs-v2 access logs.

The routing table :data:`ROUTES` is the single source of truth: :class:`MediaDistribution` renders it as CloudFront
behaviours and :func:`path_contract` as the JSON other CDNs copy (``docs/path-contract.json``). The rows come from
e2e.md §2.3 and SPEC §5.4; a deployment of this construct confirmed each of them through the
``cache-behavior-path-pattern`` field of its access logs, with gzip on ``/v1/dash/*`` and ``/v1/manifest/*`` and
cache hits on ``/v1/i-media/*`` (2026-10-16). The managed policies' contents were re-read with
``aws cloudfront get-cache-policy`` / ``get-origin-request-policy``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack, Token
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.constructs.observability import CLOUDFRONT_REGION, log_group_arn, vended_log_group

CONTRACT_VERSION: Final = 1
DEFAULT_PATH: Final = "*"
"""CloudFront's path pattern of the default behaviour (the last route)."""
SEGMENTS_HOST: Final = "segments.mediatailor.{region}.amazonaws.com"
MANIFESTS_HOST: Final = "manifests.mediatailor.{region}.amazonaws.com"
_IMPORTED_BUCKET_POLICY_WARNING: Final = "@aws-cdk/aws-cloudfront-origins:updateImportedBucketPolicyOac"


class Origin(StrEnum):
    """Origins of the distribution (also the CloudFront origin ids)."""

    MEDIA = "media"
    """The media bucket through OAC. Other CDNs chain to CloudFront instead: the bucket only admits CloudFront."""
    MANIFESTS = "mediatailor-manifests"
    """MediaTailor playback endpoint: sessions, personalised manifests, tracking, beacon redirects."""
    SEGMENTS = "mediatailor-segments"
    """MediaTailor segment endpoint: transcoded ads and re-hosted preconditioned ad playlists."""


class Methods(StrEnum):
    """Viewer methods a route accepts."""

    READ = "read"
    """GET, HEAD and OPTIONS (GET, HEAD and OPTIONS responses cached)."""
    ALL = "all"
    """Every method; MediaTailor's explicit session initialisation is a POST (GET and HEAD cached)."""


_ALLOWED_METHODS: Final[Mapping[Methods, cf.AllowedMethods]] = {
    Methods.READ: cf.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
    Methods.ALL: cf.AllowedMethods.ALLOW_ALL,
}
_CACHED_METHODS: Final[Mapping[Methods, cf.CachedMethods]] = {
    Methods.READ: cf.CachedMethods.CACHE_GET_HEAD_OPTIONS,
    Methods.ALL: cf.CachedMethods.CACHE_GET_HEAD,
}
_PREFLIGHT_HEADERS: Final = cf.ResponseHeadersPolicy.CORS_ALLOW_ALL_ORIGINS_WITH_PREFLIGHT_AND_SECURITY_HEADERS


class Cors(StrEnum):
    """Response headers a route adds."""

    MEDIA = "media"
    """The distribution's own policy: ``Access-Control-Allow-Origin: *`` overriding the origin's (MediaTailor echoes
    the request ``Origin`` with credentials, which a cache must never replay to other sites; cdn.md §2.3),
    ``Timing-Allow-Origin: *`` and ``Server-Timing`` (its ``cdn-rid`` is the log field ``x-edge-request-id``)."""
    PREFLIGHT = "preflight"
    """Managed-CORS-with-preflight-and-SecurityHeadersPolicy: uncached MediaTailor responses keep their own CORS."""


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """A CloudFront managed cache policy and its meaning for other CDNs."""

    policy: cf.ICachePolicy
    name: str
    ttl_seconds: tuple[int, int, int]
    """Minimum, default and maximum TTL; between the bounds the origin's ``Cache-Control`` wins."""
    key_query_strings: bool = False
    key_headers: tuple[str, ...] = ()
    key_cookies: bool = False
    compression: bool = False
    """Whether gzip/brotli responses are cached (and thus requested from the origin)."""

    def contract(self) -> dict[str, Any]:
        """CDN-neutral description of the policy."""
        minimum, default, maximum = self.ttl_seconds
        return {
            "min_ttl": minimum,
            "default_ttl": default,
            "max_ttl": maximum,
            "key": {
                "query_strings": "all" if self.key_query_strings else "none",
                "headers": list(self.key_headers),
                "cookies": "all" if self.key_cookies else "none",
            },
            "compression": self.compression,
            "cloudfront_policy": {"id": self.policy.cache_policy_id, "name": self.name},
        }


@dataclass(frozen=True, slots=True)
class OriginRequestPolicy:
    """A CloudFront managed origin request policy: what reaches the origin beyond the cache key."""

    policy: cf.IOriginRequestPolicy
    name: str
    headers: tuple[str, ...]
    """Forwarded viewer headers; ``"*"`` stands for all of them."""
    query_strings: bool = False
    cookies: bool = False

    def contract(self) -> dict[str, Any]:
        """CDN-neutral description of the policy."""
        return {
            "headers": list(self.headers),
            "query_strings": "all" if self.query_strings else "none",
            "cookies": "all" if self.cookies else "none",
            "cloudfront_policy": {"id": self.policy.origin_request_policy_id, "name": self.name},
        }


CACHING_OPTIMIZED_UNCOMPRESSED: Final = CachePolicy(
    cf.CachePolicy.CACHING_OPTIMIZED_FOR_UNCOMPRESSED_OBJECTS,
    "Managed-CachingOptimizedForUncompressedObjects",
    (1, 86400, 31536000),
)
CACHING_DISABLED: Final = CachePolicy(cf.CachePolicy.CACHING_DISABLED, "Managed-CachingDisabled", (0, 0, 0))
# Puts the viewer Host in the key: fine for manifests.mediatailor (verified), never for S3 or Channel Assembly.
ORIGIN_CACHE_CONTROL_QUERY_STRINGS: Final = CachePolicy(
    cf.CachePolicy.USE_ORIGIN_CACHE_CONTROL_HEADERS_QUERY_STRINGS,
    "UseOriginCacheControlHeaders-QueryStrings",
    (0, 0, 31536000),
    key_query_strings=True,
    key_headers=("host", "origin", "x-http-method", "x-http-method-override", "x-method-override"),
    key_cookies=True,
    compression=True,
)
CORS_S3_ORIGIN: Final = OriginRequestPolicy(
    cf.OriginRequestPolicy.CORS_S3_ORIGIN,
    "Managed-CORS-S3Origin",
    ("origin", "access-control-request-headers", "access-control-request-method"),
)
# Forwards Accept-Encoding, so MediaTailor gzips the playlists (verified on /v1/manifest/*, e2e.md §4.3).
ALL_VIEWER_AND_CLOUDFRONT_HEADERS: Final = OriginRequestPolicy(
    cf.OriginRequestPolicy.ALL_VIEWER_AND_CLOUDFRONT_2022,
    "Managed-AllViewerAndCloudFrontHeaders-2022-06",
    ("*",),
    query_strings=True,
    cookies=True,
)
# User-Agent and X-Forwarded-For feed ad targeting; every query string carries ADS variables and aws.* options.
MEDIATAILOR_PERSONALIZED_MANIFESTS: Final = OriginRequestPolicy(
    cf.OriginRequestPolicy.ELEMENTAL_MEDIA_TAILOR,
    "Managed-Elemental-MediaTailor-PersonalizedManifests",
    ("origin", "access-control-request-headers", "access-control-request-method", "user-agent", "x-forwarded-for"),
    query_strings=True,
)


@dataclass(frozen=True, slots=True)
class Route:
    """One row of the routing table."""

    path: str
    origin: Origin
    cache: CachePolicy
    forward: OriginRequestPolicy | None
    cors: Cors
    compress: bool
    methods: Methods
    purpose: str

    def contract(self) -> dict[str, Any]:
        """CDN-neutral description of the route; without ``forward`` only the cache key reaches the origin."""
        return {
            "path": self.path,
            "origin": self.origin.value,
            "purpose": self.purpose,
            "methods": _ALLOWED_METHODS[self.methods].methods,
            "compress": self.compress,
            "cache": self.cache.contract(),
            **({"forward": self.forward.contract()} if self.forward else {}),
            "cors": self.cors.value,
        }


ROUTES: Final[tuple[Route, ...]] = (
    Route(
        "/tm/*",
        Origin.SEGMENTS,
        CACHING_OPTIMIZED_UNCOMPRESSED,
        None,
        Cors.MEDIA,
        compress=False,
        methods=Methods.READ,
        purpose="Ad segments MediaTailor transcoded (external VAST creatives)",
    ),
    Route(
        "/preconditioned-ads/*",
        Origin.SEGMENTS,
        CACHING_OPTIMIZED_UNCOMPRESSED,
        None,
        Cors.MEDIA,
        compress=False,
        methods=Methods.READ,
        # Without this route the SGAI asset lists of preconditioned ads hit S3 and fail (e2e.md §5.4-5.5).
        purpose="Preconditioned ad playlists MediaTailor re-hosts for SGAI asset lists",
    ),
    Route(
        "/v1/i-media/*",
        Origin.MANIFESTS,
        ORIGIN_CACHE_CONTROL_QUERY_STRINGS,
        None,
        Cors.MEDIA,
        compress=True,
        methods=Methods.READ,
        purpose="SGAI media playlists: identical across sessions, cached for the origin's max-age (1 s)",
    ),
    Route(
        "/v1/dash/*",
        Origin.MANIFESTS,
        CACHING_DISABLED,
        ALL_VIEWER_AND_CLOUDFRONT_HEADERS,
        Cors.PREFLIGHT,
        compress=True,
        methods=Methods.READ,
        # Multi-period MPDs reach 30-100 KB; the /v1/* policy drops Accept-Encoding (_review.md, DASH).
        purpose="SSAI DASH manifests (per session, gzip)",
    ),
    Route(
        "/v1/manifest/*",
        Origin.MANIFESTS,
        CACHING_DISABLED,
        ALL_VIEWER_AND_CLOUDFRONT_HEADERS,
        Cors.PREFLIGHT,
        compress=True,
        methods=Methods.READ,
        purpose="SSAI HLS media playlists (per session, gzip)",
    ),
    Route(
        "/v1/*",
        Origin.MANIFESTS,
        CACHING_DISABLED,
        MEDIATAILOR_PERSONALIZED_MANIFESTS,
        Cors.PREFLIGHT,
        compress=True,
        methods=Methods.ALL,
        purpose="Session start (/v1/master, POST /v1/session), tracking, beacon redirects, interstitial asset lists",
    ),
    Route(
        DEFAULT_PATH,
        Origin.MEDIA,
        CACHING_OPTIMIZED_UNCOMPRESSED,
        CORS_S3_ORIGIN,  # S3 answers CORS preflights only when these headers reach it (cdn.md §4.1)
        Cors.MEDIA,
        compress=False,
        methods=Methods.READ,
        purpose=(
            "CMAF content and preconditioned ad segments, VAST pods and beacons (/ads), EPG feeds (/epg); "
            "content manifests under /assets are denied by the bucket policy"
        ),
    ),
)
"""Routes in precedence order (first match wins); MediaTailor paths precede any S3 glob (cdn.md §10.1)."""


def path_contract(*, region: str, media_host: str) -> dict[str, Any]:
    """The routing table as CDN-neutral JSON, for configuring another CDN in front of the same origins.

    Args:
        region: AWS region of the MediaTailor endpoints.
        media_host: host other CDNs fetch ``media`` routes from: the CloudFront distribution's own domain.
    """
    return {
        "version": CONTRACT_VERSION,
        "description": (
            "FAST Channels media delivery routes in precedence order (first match wins; the last rule is the "
            "default). Every URL players receive is relative or on the content domain, so any CDN that serves "
            "that domain with these rules delivers the same channels without MediaTailor changes."
        ),
        "origins": {
            Origin.MEDIA: {
                "host": media_host,
                "note": "Chain to CloudFront: the media bucket only admits CloudFront origin access control.",
            },
            Origin.MANIFESTS: {"host": MANIFESTS_HOST.format(region=region)},
            Origin.SEGMENTS: {"host": SEGMENTS_HOST.format(region=region)},
        },
        "rules": [route.contract() for route in ROUTES],
        "cors": {
            Cors.MEDIA: {
                "access-control-allow-origin": "*",
                "access-control-allow-headers": "*",
                "access-control-allow-methods": "GET, HEAD, OPTIONS",
                "access-control-expose-headers": "*",
                "access-control-max-age": 86400,
                "override_origin": True,
                "timing-allow-origin": "*",
                "server-timing": True,
            },
            Cors.PREFLIGHT: {
                "access-control-allow-origin": "*",
                "access-control-allow-methods": "GET, HEAD, PUT, POST, PATCH, DELETE, OPTIONS",
                "access-control-expose-headers": "*",
                "override_origin": False,
            },
        },
        "notes": [
            "A rule without 'forward' sends the origin only what its cache key contains.",
            "Send each origin its own host name as Host; CloudFront forwards the viewer Host to MediaTailor only "
            "where a policy lists it, and that was verified with *.cloudfront.net hosts only.",
            "Never cache /v1/master, /v1/session or /v1/segment responses: they create sessions or expire.",
            "Keep CMCD query strings and headers out of segment cache keys.",
        ],
    }


class MediaDistribution(Construct):
    """The viewer-facing CloudFront distribution: S3 content through OAC and MediaTailor SSAI/SGAI, routed by
    :data:`ROUTES`, over HTTP/2 and HTTP/3 with IPv6.

    The media bucket's policy belongs to its owner: pass an imported reference (``DataExports.media_bucket``)
    whose policy comes from ``MediaStorage.allow_cloudfront_read()``, which admits every distribution of the
    account and denies content manifests. CDK then adds no statement of its own (its "imported bucket" warning is
    acknowledged here). MediaTailor endpoints are public; ``mediatailor_oac_id`` attaches an origin access control
    to both of them once CloudFront offers a MediaTailor type (none exists today: cdn.md §1).

    Policies: managed ones everywhere, plus one custom response headers policy (:attr:`Cors.MEDIA`) and no custom
    cache policy, because accounts have only 20 of each.

    Args:
        media_bucket: bucket holding ``assets/``, ``ads/`` and ``epg/``.
        content_domain: CDN-neutral host name of content and ad segments (e.g. ``media.example.com``), served as
            an alternate domain name of this distribution. Requires ``certificate_arn``.
        certificate_arn: ACM certificate in us-east-1 covering ``content_domain``.
        price_class: edge locations to serve from.
        mediatailor_oac_id: origin access control for the MediaTailor origins.
        publish_additional_metrics: CloudFront's paid per-distribution metrics (cache hit rate, origin latency).

    Attributes:
        distribution: the CloudFront distribution.
        domain_name: ``content_domain``, or the distribution's ``*.cloudfront.net`` name.
        cdn_base_url: ``https://<domain_name>``: playback URLs (MediaTailor paths) and console media.
        content_base_url: ``https://<domain_name>``: Channel Assembly's default segment delivery, the ad segment
            prefix and VAST/ADS URLs. Identical to ``cdn_base_url`` in v3.0; the names keep the playback CDN and the
            CDN-neutral content host apart, as ``MediaExports`` does.
        path_contract: :func:`path_contract` for this distribution (also the ``PathContract`` stack output).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        media_bucket: s3.IBucket,
        content_domain: str | None = None,
        certificate_arn: str | None = None,
        price_class: cf.PriceClass = cf.PriceClass.PRICE_CLASS_ALL,
        mediatailor_oac_id: str | None = None,
        publish_additional_metrics: bool = False,
    ) -> None:
        super().__init__(scope, id)
        if (content_domain is None) != (certificate_arn is None):
            raise ValueError("content_domain and certificate_arn must be given together")
        stack = Stack.of(self)
        self.cors_policy = cf.ResponseHeadersPolicy(
            self,
            "MediaCors",
            comment="FAST Channels media: CORS * (overriding the origin), Timing-Allow-Origin, Server-Timing",
            cors_behavior=cf.ResponseHeadersCorsBehavior(
                access_control_allow_origins=["*"],
                access_control_allow_headers=["*"],  # CMCD header mode preflights (cdn.md §2.3)
                access_control_allow_methods=["GET", "HEAD", "OPTIONS"],
                access_control_allow_credentials=False,
                access_control_expose_headers=["*"],
                access_control_max_age=Duration.days(1),
                origin_override=True,
            ),
            custom_headers_behavior=cf.ResponseCustomHeadersBehavior(
                custom_headers=[cf.ResponseCustomHeader(header="Timing-Allow-Origin", value="*", override=True)]
            ),
            server_timing_sampling_rate=100,
        )
        targets = _origins(media_bucket, region=stack.region, mediatailor_oac_id=mediatailor_oac_id)
        behaviours = {route.path: self._behaviour(route, targets) for route in ROUTES}
        self.distribution = cf.Distribution(
            self,
            "Distribution",
            comment="FAST Channels: media delivery (S3 content, MediaTailor SSAI and SGAI)",
            default_behavior=behaviours.pop(DEFAULT_PATH),
            additional_behaviors=behaviours,
            http_version=cf.HttpVersion.HTTP2_AND_3,
            enable_ipv6=True,
            price_class=price_class,
            publish_additional_metrics=publish_additional_metrics or None,
            **_custom_domain(self, content_domain, certificate_arn),
        )
        Annotations.of(self.distribution).acknowledge_warning(
            _IMPORTED_BUCKET_POLICY_WARNING, "MediaStorage.allow_cloudfront_read() owns the media bucket policy"
        )
        findings = {
            "AwsSolutions-CFR1": "FAST audiences are global; add a geo restriction where content rights need one",
            "AwsSolutions-CFR2": "No WAF in v3.0: web ACLs bill per segment request (cdn.md §5.3)",
            "AwsSolutions-CFR3": "Standard logging v2 (CdnAccessLogs) replaces the legacy logging this rule checks",
        }
        if content_domain is None:
            findings["AwsSolutions-CFR4"] = (
                "The *.cloudfront.net certificate fixes the viewer TLS policy; a content_domain gets TLSv1.2_2025"
            )
        nag.acknowledge(self.distribution, findings)
        self.domain_name: str = content_domain or self.distribution.distribution_domain_name
        self.cdn_base_url = f"https://{self.domain_name}"
        self.content_base_url = self.cdn_base_url
        self.path_contract = path_contract(region=stack.region, media_host=self.distribution.distribution_domain_name)
        for key, value, description in (
            ("CdnBaseUrl", self.cdn_base_url, "Base URL of playback and media through CloudFront"),
            ("DistributionId", self.distribution.distribution_id, "Media CloudFront distribution"),
            ("PathContract", stack.to_json_string(self.path_contract), "Routes for configuring another CDN"),
        ):
            CfnOutput(self, key, key=key, value=value, description=description)

    def _behaviour(self, route: Route, targets: Mapping[Origin, cf.IOrigin]) -> cf.BehaviorOptions:
        cors = self.cors_policy if route.cors is Cors.MEDIA else _PREFLIGHT_HEADERS
        return cf.BehaviorOptions(
            origin=targets[route.origin],
            cache_policy=route.cache.policy,
            origin_request_policy=route.forward.policy if route.forward else None,
            response_headers_policy=cors,
            compress=route.compress,
            allowed_methods=_ALLOWED_METHODS[route.methods],
            cached_methods=_CACHED_METHODS[route.methods],
            viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
        )


def _origins(bucket: s3.IBucket, *, region: str, mediatailor_oac_id: str | None) -> dict[Origin, cf.IOrigin]:
    def mediatailor(origin: Origin, host: str) -> cf.IOrigin:
        # Timeouts and retries as deployed in e2e.md §2.3.
        return origins.HttpOrigin(
            host.format(region=region),
            origin_id=origin.value,
            protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cf.OriginSslPolicy.TLS_V1_2],
            read_timeout=Duration.seconds(10),
            keepalive_timeout=Duration.seconds(5),
            connection_attempts=3,
            connection_timeout=Duration.seconds(10),
            origin_access_control_id=mediatailor_oac_id,
        )

    return {
        Origin.MEDIA: origins.S3BucketOrigin.with_origin_access_control(
            bucket, origin_id=Origin.MEDIA.value, origin_access_levels=[cf.AccessLevel.READ]
        ),
        Origin.MANIFESTS: mediatailor(Origin.MANIFESTS, MANIFESTS_HOST),
        Origin.SEGMENTS: mediatailor(Origin.SEGMENTS, SEGMENTS_HOST),
    }


def _custom_domain(scope: Construct, domain: str | None, certificate_arn: str | None) -> dict[str, Any]:
    if domain is None or certificate_arn is None:
        return {}  # the *.cloudfront.net certificate pins the viewer TLS policy (cdn.md §4)
    return {
        "domain_names": [domain],
        "certificate": acm.Certificate.from_certificate_arn(scope, "Certificate", certificate_arn),
        "minimum_protocol_version": cf.SecurityPolicyProtocol.TLS_V1_2_2025,
    }


ACCESS_LOG_FIELDS: Final[tuple[str, ...]] = (
    "timestamp(ms)",
    "DistributionId",
    "x-edge-request-id",  # = Server-Timing cdn-rid = x-amz-cf-id seen by the player
    "x-edge-location",
    "c-ip",
    "c-country",
    "asn",
    "cs-method",
    "cs(Host)",
    "cs-uri-stem",
    "cs-uri-query",  # ADS requests carry ch, sid, evid and upid (SPEC §9)
    "cs(User-Agent)",
    "cs(Referer)",
    "x-forwarded-for",
    "cs-protocol",
    "cs-protocol-version",
    "ssl-protocol",
    "sc-status",
    "sc-bytes",
    "sc-content-type",
    "sc-range-start",
    "sc-range-end",
    "x-edge-result-type",
    "x-edge-detailed-result-type",
    "x-edge-response-result-type",
    "time-taken",
    "time-to-first-byte",
    "origin-fbl",
    "origin-lbl",
    "cache-behavior-path-pattern",
    # CMCD, parsed from query and header mode alike (cdn.md §5.1); sid joins MediaTailor's sessionId.
    "cmcd-session-id",
    "cmcd-content-id",
    "cmcd-object-type",
    "cmcd-object-duration",
    "cmcd-encoded-bitrate",
    "cmcd-top-bitrate",
    "cmcd-measured-throughput",
    "cmcd-requested-maximum-throughput",
    "cmcd-buffer-length",
    "cmcd-buffer-starvation",
    "cmcd-deadline",
    "cmcd-playback-rate",
    "cmcd-startup",
    "cmcd-streaming-format",
    "cmcd-stream-type",
    "cmcd-next-object-request",
    "cmcd-next-range-request",
    "cmcd-version",
)
"""Standard logs v2 record fields (``aws logs describe-configuration-templates --service cloudfront``); cookies
and field-level-encryption fields are left out."""
S3_SUFFIX_PATH: Final = "{DistributionId}/{yyyy}/{MM}/{dd}/{HH}"


class CdnAccessLogs(Construct):
    """CloudFront standard logging v2 of one distribution: Parquet objects with Hive-style partitions in the logs
    bucket (``AWSLogs/aws-account-id=<acct>/CloudFront/DistributionId=<id>/year=…/month=…/day=…/hour=…/``; Parquet
    column names use ``_`` for ``-``, ``(`` and ``)``, e.g. ``cache_behavior_path_pattern``, ``cs_User_Agent``) and,
    optionally, JSON records in an Infrequent Access log group for Logs Insights (no FilterLogEvents on that class).

    CloudFront's log delivery sources exist only in us-east-1, so this construct must be in a us-east-1 stack: the
    Media stack when the deployment runs there, the Edge stack otherwise. The bucket may be in any region; its
    policy comes from ``MediaStorage.allow_log_delivery()``. Only stable L1 resources are used (the logs-delivery
    mixins are experimental and add an account-level resource policy each; cdn.md §5.1).

    Args:
        distribution: the distribution to log.
        logs_bucket: bucket receiving the Parquet objects.
        name: prefix of the delivery source, destination and log-group names, unique per account (the deployment
            prefix).
        cloudwatch_retention: also deliver to ``/aws/vendedlogs/cloudfront/<name>`` with this retention.
        removal_policy: what happens to that log group when the stack is deleted.

    Attributes:
        source: the delivery source.
        log_group: the Logs Insights log group, if ``cloudwatch_retention`` is set.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        distribution: cf.IDistribution,
        logs_bucket: s3.IBucket,
        name: str,
        cloudwatch_retention: logs.RetentionDays | None = None,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        if not Token.is_unresolved(stack.region) and stack.region != CLOUDFRONT_REGION:
            raise ValueError(f"CdnAccessLogs must be in a {CLOUDFRONT_REGION} stack, not {stack.region}")
        self.source = logs.CfnDeliverySource(
            self,
            "Source",
            name=f"{name}-cdn-access",
            log_type="ACCESS_LOGS",
            resource_arn=distribution.distribution_arn,
        )
        to_s3 = self._deliver(
            "S3",
            destination_name=f"{name}-cdn-access-s3",
            destination_arn=logs_bucket.bucket_arn,
            output_format="parquet",
            s3_suffix_path=S3_SUFFIX_PATH,
            s3_enable_hive_compatible_path=True,
        )
        self.log_group: logs.LogGroup | None = None
        if cloudwatch_retention is not None:
            self.log_group = vended_log_group(
                self,
                "Logs",
                name=f"/aws/vendedlogs/cloudfront/{name}",
                delivery_sources=f"{name}-cdn-*",
                retention=cloudwatch_retention,
                removal_policy=removal_policy,
                log_group_class=logs.LogGroupClass.INFREQUENT_ACCESS,
            )
            to_logs = self._deliver(
                "Logs",
                destination_name=f"{name}-cdn-access-logs",
                destination_arn=log_group_arn(self, self.log_group.log_group_name),
                output_format="json",
            )
            to_logs.add_resource_dependency(to_s3)  # deliveries of one source are created one at a time

    def _deliver(
        self, id: str, *, destination_name: str, destination_arn: str, output_format: str, **options: Any
    ) -> logs.CfnDelivery:
        destination = logs.CfnDeliveryDestination(
            self,
            f"{id}Destination",
            name=destination_name,
            destination_resource_arn=destination_arn,
            output_format=output_format,
        )
        return logs.CfnDelivery(
            self,
            f"{id}Delivery",
            delivery_source_name=self.source.ref,
            delivery_destination_arn=destination.attr_arn,
            record_fields=list(ACCESS_LOG_FIELDS),
            **options,
        )
