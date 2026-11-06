"""Media delivery (SPEC §5.4): one CloudFront distribution in front of the media bucket and MediaTailor, its
CDN-neutral path contract, and its standard-logs-v2 access logs.

The routing table :data:`ROUTES` is the single source of truth: :class:`MediaDistribution` renders it as CloudFront
behaviours and :func:`path_contract` as the JSON other CDNs copy (``docs/path-contract.json``). The rows follow
MediaTailor's production CloudFront configuration (MediaTailor User Guide, "Production-ready CloudFront configuration
for MediaTailor": ``/tm/*`` to the segments origin, ``/v1/*`` uncached to the manifests origin, the content origin as
the default); a deployment of this construct confirmed each of them through the ``cache-behavior-path-pattern`` field
of its access logs. The managed policies' contents were re-read with ``aws cloudfront get-cache-policy`` /
``get-origin-request-policy``. Ad insertion is SSAI only, so the guide's SGAI route (``/v1/i-media/*``) is left out,
and MediaTailor's server-side beacon redirects are ``/v1/segment/*`` requests, which ``/v1/*`` covers.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final

from aws_cdk import Annotations, Duration, Stack, Token
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.core.pbs_config import TRACKER_PREFIX
from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import CLOUDFRONT_REGION
from fast_channels.infra.constructs.logs import deliver

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
    """MediaTailor segment endpoint: ad segments MediaTailor transcoded (``/tm/*``)."""
    CATALOG = "catalog"
    """The catalog-context bucket of the ``crawl`` module through OAC. Its policy lets this distribution list the
    bucket, so S3 answers 404 for a missing object, which MediaTailor's Function cache stores (a 403 it does not)."""


class Methods(StrEnum):
    """Viewer methods a route accepts."""

    READ = "read"
    """GET, HEAD and OPTIONS (GET, HEAD and OPTIONS responses cached)."""
    ALL = "all"
    """Every method; MediaTailor's explicit session initialisation is a POST (GET and HEAD cached)."""
    GET_HEAD = "get-head"
    """GET and HEAD only (both cached): server-to-server reads that no browser preflights."""


_ALLOWED_METHODS: Final[Mapping[Methods, cf.AllowedMethods]] = {
    Methods.READ: cf.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
    Methods.ALL: cf.AllowedMethods.ALLOW_ALL,
    Methods.GET_HEAD: cf.AllowedMethods.ALLOW_GET_HEAD,
}
_CACHED_METHODS: Final[Mapping[Methods, cf.CachedMethods]] = {
    Methods.READ: cf.CachedMethods.CACHE_GET_HEAD_OPTIONS,
    Methods.ALL: cf.CachedMethods.CACHE_GET_HEAD,
    Methods.GET_HEAD: cf.CachedMethods.CACHE_GET_HEAD,
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
CORS_S3_ORIGIN: Final = OriginRequestPolicy(
    cf.OriginRequestPolicy.CORS_S3_ORIGIN,
    "Managed-CORS-S3Origin",
    ("origin", "access-control-request-headers", "access-control-request-method"),
)
# Forwards every viewer header, including the four MediaTailor requires from a CDN (MediaTailor User Guide, "Required
# headers for MediaTailor CDN integration"): Accept-Encoding (it gzips the playlists, verified on /v1/manifest/...),
# User-Agent and X-Forwarded-For (ad targeting) and Host (request routing); and every query string (ADS variables,
# aws.* session options). Nothing behind /v1/* is cached, so forwarding everything cannot fragment a cache.
ALL_VIEWER_AND_CLOUDFRONT_HEADERS: Final = OriginRequestPolicy(
    cf.OriginRequestPolicy.ALL_VIEWER_AND_CLOUDFRONT_2022,
    "Managed-AllViewerAndCloudFrontHeaders-2022-06",
    ("*",),
    query_strings=True,
    cookies=True,
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
    viewer_request: str | None = None
    """What a viewer-request function on the route does (another CDN must do the same), or None."""

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
            **({"viewer_request": self.viewer_request} if self.viewer_request else {}),
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
        "/v1/*",
        Origin.MANIFESTS,
        CACHING_DISABLED,
        ALL_VIEWER_AND_CLOUDFRONT_HEADERS,
        Cors.PREFLIGHT,
        compress=True,
        methods=Methods.ALL,
        # Every stitched ad segment is a /v1/segment 301 to the CDN that fires the server-side beacons (e2e.md §4.3).
        purpose=(
            "SSAI sessions: multivariant (/v1/master), media playlists (/v1/manifest, gzip), POST /v1/session, "
            "tracking, ad segment redirects (/v1/segment)"
        ),
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
            "MPEG-TS content and preconditioned ad segments; ad manifest copies, VAST pods and beacons (/ads); EPG "
            "feeds (/epg); content manifests under /assets are denied by the bucket policy"
        ),
    ),
)
"""Routes in precedence order (first match wins); MediaTailor paths precede any S3 glob (cdn.md §10.1)."""

CATALOG_CONTEXT_PATH: Final = "/catalog/ctx/*"
CATALOG_CONTEXT_ROUTE: Final = Route(
    CATALOG_CONTEXT_PATH,
    Origin.CATALOG,
    CACHING_OPTIMIZED_UNCOMPRESSED,  # no query string, header or cookie in the key, so none reaches the bucket
    None,
    Cors.MEDIA,
    compress=False,
    methods=Methods.GET_HEAD,
    purpose=(
        "Catalog context objects of the crawl module (/catalog/ctx/v1/<content id>.json), read by MediaTailor's "
        "PRE_ADS lookup; a missing object is a 404, which the Functions cache stores"
    ),
)
"""The ``crawl`` module's route. Every request it forwards has a path under ``/catalog/ctx/`` and no query string,
so S3 sees a ``GetObject`` of a key under ``catalog/ctx/``: never the bucket root, never a listing."""


MEDIATAILOR_PATH: Final = "/v1/*"
EDGE_REBUILD: Final = (
    "The identity module's edge rebuild: the query is emptied, an init request (/v1/master/<40 hex>/<configuration>/"
    "<name>.m3u8, or /v1/dash/…/<name>.mpd) gets back only a single ads.t of 22 URL-safe characters, /v1/session is "
    "refused (403), a non-canonical init path too (400), and other paths keep their query"
)
"""What the ``/v1/*`` route's viewer-request function does with the identity module on
(``constructs.identity.edge``)."""


def _ticketed(route: Route) -> Route:
    """The ``/v1/*`` route with the identity module: reads only, behind the edge rebuild."""
    return replace(
        route,
        methods=Methods.READ,
        purpose=(
            "SSAI sessions: multivariant (/v1/master), media playlists (/v1/manifest, gzip), tracking, ad segment "
            "redirects (/v1/segment); no explicit session initialization"
        ),
        viewer_request=EDGE_REBUILD,
    )


TRACKER_GIF: Final = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAICRAEAOw=="
"""Base64 of the 1x1 GIF the tracker route answers: the kit's beacon object's bytes (``ingest.kit.BEACON_GIF``)."""
TRACKER_FUNCTION_CODE: Final = (
    "function handler(event) {\n"
    "  return {\n"
    "    statusCode: 200,\n"
    "    statusDescription: 'OK',\n"
    "    headers: {'content-type': {value: 'image/gif'}, 'cache-control': {value: 'no-store'}},\n"
    f"    body: {{encoding: 'base64', data: '{TRACKER_GIF}'}},\n"
    "  };\n"
    "}\n"
)
"""The tracker route's viewer-request function (cloudfront-js-2.0): the GIF, for every request, without the origin."""
TRACKER_ROUTE: Final = Route(
    f"{TRACKER_PREFIX}*",
    Origin.MEDIA,  # never reached: the function answers
    CACHING_DISABLED,
    None,
    Cors.MEDIA,
    compress=False,
    methods=Methods.READ,
    purpose=(
        "Our impression tracker, /ads/b/<sid>.<evid>/<crid>/<enr>.gif, which MediaTailor fires server-side: its "
        "parameters are in the path, which the access logs keep (delivery counts from them, W-2 1.32)"
    ),
    viewer_request="Answers a 1x1 GIF (image/gif, Cache-Control: no-store) to every request, without the origin",
)
"""The ``demand`` module's route: the fast-ctv module and the Pod Decision Service render our tracker on it. The path
does not exist in the media bucket, whose answer would be a 403 that MediaTailor retries; the access logs record the
request's path as the viewer sent it."""


def routes(*, catalog_context: bool = False, ticketed: bool = False, tracker: bool = False) -> tuple[Route, ...]:
    """The routing table of a deployment: :data:`ROUTES`, with the ``/v1/*`` route behind the edge rebuild when the
    ``identity`` module is on, and the tracker route (the ``demand`` module) and the catalog-context route (the
    ``crawl`` module) before the default one."""
    table = tuple(_ticketed(r) if ticketed and r.path == MEDIATAILOR_PATH else r for r in ROUTES)
    extra = (*((TRACKER_ROUTE,) if tracker else ()), *((CATALOG_CONTEXT_ROUTE,) if catalog_context else ()))
    if not extra:
        return table
    return (*table[:-1], *extra, table[-1])


def path_contract(*, region: str, media_host: str, table: tuple[Route, ...] = ROUTES) -> dict[str, Any]:
    """The routing table as CDN-neutral JSON, for configuring another CDN in front of the same origins.

    Args:
        region: AWS region of the MediaTailor endpoints.
        media_host: host other CDNs fetch ``media`` (and ``catalog``) routes from: the CloudFront distribution's own
            domain.
        table: the deployment's routes (:func:`routes`).
    """
    catalog = (
        {
            Origin.CATALOG: {
                "host": media_host,
                "note": "Chain to CloudFront: the catalog-context bucket only admits this distribution.",
            }
        }
        if any(route.origin is Origin.CATALOG for route in table)
        else {}
    )
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
            **catalog,
        },
        "rules": [route.contract() for route in table],
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
    """The viewer-facing CloudFront distribution: S3 content through OAC and MediaTailor SSAI, routed by
    :data:`ROUTES`, over HTTP/2 and HTTP/3 with IPv6.

    The media bucket's policy belongs to its owner: pass an imported reference (``DataExports.media_bucket``)
    whose policy comes from ``MediaStorage.allow_cloudfront_read()``, which admits every distribution of the
    account and denies content manifests. CDK then adds no statement of its own (its "imported bucket" warning is
    acknowledged here). MediaTailor endpoints are public: CloudFront has no origin access control type for them
    (cdn.md §1).

    Policies: managed ones everywhere, plus one custom response headers policy (:attr:`Cors.MEDIA`) and no custom
    cache policy, because accounts have only 20 of each.

    Args:
        media_bucket: bucket holding ``assets/``, ``ads/`` and ``epg/``.
        catalog_bucket: the ``crawl`` module's catalog-context bucket; adds :data:`CATALOG_CONTEXT_ROUTE` to it. Pass
            an imported reference: its owner writes the policy, which must name this distribution (CDK adds none).
        content_domain: CDN-neutral host name of content and ad segments (e.g. ``media.example.com``), served as
            an alternate domain name of this distribution. Requires ``certificate_arn``.
        certificate_arn: ACM certificate in us-east-1 covering ``content_domain``.
        price_class: edge locations to serve from.
        publish_additional_metrics: CloudFront's paid per-distribution metrics (cache hit rate, origin latency).
        edge_rebuild: the ``identity`` module's viewer-request function; puts the ``/v1/*`` route behind it, reads
            only (``constructs.identity.edge``).
        tracker: the ``demand`` module's tracker route (:data:`TRACKER_ROUTE`) and its CloudFront Function.

    Attributes:
        distribution: the CloudFront distribution.
        domain_name: ``content_domain``, or the distribution's ``*.cloudfront.net`` name.
        cdn_base_url: ``https://<domain_name>``: playback URLs (MediaTailor paths) and console media.
        content_base_url: ``https://<domain_name>``: Channel Assembly's default segment delivery, the ad segment
            prefix and VAST/ADS URLs. Identical to ``cdn_base_url`` in v3.0; the names keep the playback CDN and the
            CDN-neutral content host apart, as ``MediaExports`` does.
        routes: the distribution's routing table (:func:`routes`).
        path_contract: :func:`path_contract` for this distribution (``MediaStack`` outputs it as ``PathContract``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        media_bucket: s3.IBucket,
        catalog_bucket: s3.IBucket | None = None,
        content_domain: str | None = None,
        certificate_arn: str | None = None,
        price_class: cf.PriceClass = cf.PriceClass.PRICE_CLASS_ALL,
        publish_additional_metrics: bool = False,
        edge_rebuild: cf.IFunction | None = None,
        tracker: bool = False,
    ) -> None:
        super().__init__(scope, id)
        self.functions: dict[str, cf.IFunction] = {}
        """The viewer-request function of each route that has one, by path."""
        if edge_rebuild is not None:
            self.functions[MEDIATAILOR_PATH] = edge_rebuild
        if tracker:
            # OPTIMIZATION(aws): W-2 1.32 the tracker's parameters in a path the logs keep: the ADS log can drop a break
            self.functions[TRACKER_ROUTE.path] = cf.Function(
                self,
                "TrackerFunction",
                comment="FAST Channels: our impression tracker /ads/b/*, a 1x1 GIF at the edge (no origin)",
                code=cf.FunctionCode.from_inline(TRACKER_FUNCTION_CODE),
                runtime=cf.FunctionRuntime.JS_2_0,
                auto_publish=True,
            )
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
        targets = _origins(media_bucket, region=stack.region, catalog_bucket=catalog_bucket)
        self.routes = routes(
            catalog_context=catalog_bucket is not None, ticketed=edge_rebuild is not None, tracker=tracker
        )
        behaviours = {route.path: self._behaviour(route, targets) for route in self.routes}
        self.distribution = cf.Distribution(
            self,
            "Distribution",
            comment="FAST Channels: media delivery (S3 content, MediaTailor SSAI)",
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
        self.path_contract = path_contract(
            region=stack.region, media_host=self.distribution.distribution_domain_name, table=self.routes
        )

    def _behaviour(self, route: Route, targets: Mapping[Origin, cf.IOrigin]) -> cf.BehaviorOptions:
        cors = self.cors_policy if route.cors is Cors.MEDIA else _PREFLIGHT_HEADERS
        viewer_request = self.functions.get(route.path)
        return cf.BehaviorOptions(
            origin=targets[route.origin],
            cache_policy=route.cache.policy,
            origin_request_policy=route.forward.policy if route.forward else None,
            response_headers_policy=cors,
            compress=route.compress,
            allowed_methods=_ALLOWED_METHODS[route.methods],
            cached_methods=_CACHED_METHODS[route.methods],
            viewer_protocol_policy=cf.ViewerProtocolPolicy.HTTPS_ONLY,
            function_associations=[
                cf.FunctionAssociation(function=viewer_request, event_type=cf.FunctionEventType.VIEWER_REQUEST)
            ]
            if viewer_request is not None
            else None,
        )


def _origins(bucket: s3.IBucket, *, region: str, catalog_bucket: s3.IBucket | None) -> dict[Origin, cf.IOrigin]:
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
        )

    return {
        Origin.MEDIA: origins.S3BucketOrigin.with_origin_access_control(
            bucket, origin_id=Origin.MEDIA.value, origin_access_levels=[cf.AccessLevel.READ]
        ),
        Origin.MANIFESTS: mediatailor(Origin.MANIFESTS, MANIFESTS_HOST),
        Origin.SEGMENTS: mediatailor(Origin.SEGMENTS, SEGMENTS_HOST),
        **(
            {
                Origin.CATALOG: origins.S3BucketOrigin.with_origin_access_control(
                    catalog_bucket, origin_id=Origin.CATALOG.value, origin_access_levels=[cf.AccessLevel.READ]
                )
            }
            if catalog_bucket is not None
            else {}
        ),
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
VIEWER_ACCESS_LOG_FIELDS: Final = frozenset({"c-ip", "cs-uri-query", "x-forwarded-for"})
"""The viewer's address and original query: ``cs-uri-query`` is recorded before any edge rewrite, so it holds the
playback ticket. The lake never reads them, and with the ``identity`` module the delivery leaves them out."""
S3_SUFFIX_PATH: Final = "{DistributionId}/{yyyy}/{MM}/{dd}/{HH}"


class CdnAccessLogs(Construct):
    """CloudFront standard logging v2 of one distribution: Parquet objects with Hive-style partitions in the logs
    bucket (``AWSLogs/aws-account-id=<acct>/CloudFront/DistributionId=<id>/year=…/month=…/day=…/hour=…/``; Parquet
    column names use ``_`` for ``-``, ``(`` and ``)``, e.g. ``cache_behavior_path_pattern``, ``cs_User_Agent``), for
    Athena.

    CloudFront's log delivery sources exist only in us-east-1, so this construct must be in a us-east-1 stack: the
    Media stack when the deployment runs there, the Edge stack otherwise. The bucket may be in any region; its
    policy comes from ``MediaStorage.allow_log_delivery()``. Only stable L1 resources are used (the logs-delivery
    mixins are experimental and add an account-level resource policy each; cdn.md §5.1).

    Args:
        distribution: the distribution to log.
        logs_bucket: bucket receiving the Parquet objects.
        name: prefix of the delivery source and destination names, unique per account (the deployment prefix).
        private: leave out :data:`VIEWER_ACCESS_LOG_FIELDS` (the ``identity`` module: the original query holds the
            playback ticket).

    Attributes:
        source: the delivery source.
        delivery: its delivery to the logs bucket.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        distribution: cf.IDistribution,
        logs_bucket: s3.IBucket,
        name: str,
        private: bool = False,
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
        destination = logs.CfnDeliveryDestination(
            self,
            "S3Destination",
            name=f"{name}-cdn-access-s3",
            destination_resource_arn=logs_bucket.bucket_arn,
            output_format="parquet",
        )
        self.delivery = deliver(
            self,
            "S3Delivery",
            source=self.source,
            destination_arn=destination.attr_arn,
            record_fields=[f for f in ACCESS_LOG_FIELDS if not (private and f in VIEWER_ACCESS_LOG_FIELDS)],
            s3_suffix_path=S3_SUFFIX_PATH,
            s3_enable_hive_compatible_path=True,
        )
