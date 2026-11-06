"""Operator console hosting: the console/dist SPA in a private bucket behind its own CloudFront distribution."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Stack

from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs import static_site
from fast_channels.infra.constructs.static_site import ENTRY_OBJECTS, IMMUTABLE, SiteText, StaticSite
from fast_channels.infra.exports import channel_playback

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.exports import ChannelExports

__all__ = [
    "CONSOLE_DIST",
    "ENTRY_OBJECTS",
    "IMMUTABLE",
    "PLACEHOLDER",
    "ConsoleConfig",
    "OperatorConsole",
    "channel_playback",
    "content_security_policy",
]

CONSOLE_DIST: Final = REPO_ROOT / "console" / "dist"
"""Output of ``cd console && npm ci && npm run build`` (built before synth, never during it)."""
PLACEHOLDER: Final = (
    "<!doctype html><title>FAST Channels</title><p>Console not built: run <code>npm ci &amp;&amp; npm run build</code>"
    " in <code>console/</code> and deploy again.</p>"
)
TEXT: Final = SiteText(
    name="operator console",
    build="console",
    build_command="cd console && npm ci && npm run build",
    placeholder=PLACEHOLDER,
    bucket="Holds only the public console build; reads arrive through CloudFront",
    distribution={
        "AwsSolutions-CFR1": "The console is for authenticated operators anywhere; no geo restriction",
        "AwsSolutions-CFR2": "Static SPA assets only; every API it calls authorizes Cognito tokens itself",
        "AwsSolutions-CFR3": "Static SPA assets only; operator actions are logged by the Gateway and Runtime",
        "AwsSolutions-CFR4": "The default *.cloudfront.net certificate; no custom domain for the console",
    },
)


def content_security_policy(*, region: str, url_suffix: str, connect_sources: Sequence[str]) -> str:
    """The console's CSP (console/README.md, verified in Playwright under the production bundle).

    :func:`~fast_channels.infra.constructs.static_site.content_security_policy` with ``connect-src`` covering
    ``connect_sources`` (the media CDN and others), the Runtime, the Gateway and Cognito. No MediaTailor host: SSAI
    players fetch everything through the CDN (MediaTailor writes relative manifest URIs, and the ``/v1/segment`` ad
    segment redirects point back at the CDN).
    """
    aws = f"{region}.{url_suffix}"
    return static_site.content_security_policy(
        [
            *connect_sources,
            f"https://bedrock-agentcore.{aws}",
            f"https://*.gateway.bedrock-agentcore.{aws}",
            f"https://cognito-idp.{aws}",
        ]
    )


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
                    "playback": channel.playback(self.cdn_base_url),
                }
                for channel in self.channels
            ],
        }


class OperatorConsole(StaticSite):
    """The operator console's :class:`~fast_channels.infra.constructs.static_site.StaticSite`, with the console's CSP.

    Content is deployed by :meth:`publish`, after the backend it points at exists.

    Args:
        cognito_domain_url: managed login domain (``https://<prefix>.auth.<region>.amazoncognito.com``).
        media_urls: media CDN and content base URLs the players fetch from.
        connect_sources: further ``connect-src`` sources, e.g. another media CDN (ad beacons need none: MediaTailor
            fires them server-side).
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
        stack = Stack.of(scope)
        csp = content_security_policy(
            region=stack.region,
            url_suffix=stack.url_suffix,
            connect_sources=[*media_urls, cognito_domain_url, *connect_sources],
        )
        super().__init__(scope, id, csp=csp, dist=dist, text=TEXT)

    def publish(self, config: ConsoleConfig) -> None:
        """Deploy the build and ``config.json`` (CONTRACTS §H), then invalidate the entry objects."""
        self.publish_document(config.document())
