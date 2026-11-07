"""The viewer app's site: the ``console/dist-viewer`` build behind its own CloudFront distribution, and its
``config.json`` (validated by console/src/viewer/config.ts)."""

import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Fn, Stack

from fast_channels.core.monetization.names import plan_key
from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs import static_site
from fast_channels.infra.constructs.static_site import SiteText, StaticSite

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.core.personas import Persona
    from fast_channels.infra.exports import ChannelExports

VIEWER_DIST: Final = REPO_ROOT / "console" / "dist-viewer"
"""Output of ``cd console && npm ci && npm run build`` (the viewer's build, beside the console's ``dist``)."""
PLACEHOLDER: Final = (
    "<!doctype html><title>FAST Channels viewer</title><p>Viewer not built: run <code>npm ci &amp;&amp; npm run"
    " build</code> in <code>console/</code> and deploy again.</p>"
)
TEXT: Final = SiteText(
    name="reference viewer",
    build="viewer",
    build_command="cd console && npm ci && npm run build",
    placeholder=PLACEHOLDER,
    bucket="Holds only the public viewer build; reads arrive through CloudFront",
    distribution={
        "AwsSolutions-CFR1": "A demo viewer for anyone anywhere; no geo restriction",
        "AwsSolutions-CFR2": "Static SPA assets only; every API it calls authorizes viewer-pool tokens itself",
        "AwsSolutions-CFR3": "Static SPA assets only; playback is logged by the media CDN and the APIs",
        "AwsSolutions-CFR4": "The default *.cloudfront.net certificate; no custom domain for the demo viewer",
    },
)


def origin(url: str) -> str:
    """``https://<host>`` of an ``https://<host>/<path>`` URL, which may be a token (an API's URL)."""
    return f"https://{Fn.select(2, Fn.split('/', url))}"


def viewer_csp(*, region: str, url_suffix: str, connect_sources: Sequence[str]) -> str:
    """The viewer's CSP: the console's directives, with ``connect-src`` covering ``connect_sources`` (the media CDN, the
    content base, the playback API, the ad panel API) and the Cognito user-pools API that sign-in calls. No AgentCore
    host, no hosted login page and no third party."""
    return static_site.content_security_policy([*connect_sources, f"https://cognito-idp.{region}.{url_suffix}"])


@dataclass(frozen=True, slots=True)
class ViewerConfig:
    """``config.json`` of the viewer app (console/src/viewer/config.ts)."""

    region: str
    user_pool_client_id: str
    """The viewer pool's public client (USER_PASSWORD_AUTH)."""
    playback_url: str
    """``POST`` here mints a ticket (the identity module's ``POST /playback``)."""
    cdn_base_url: str
    ad_panel_url: str | None
    """The demo's ad panel API, or ``None`` without ``modules.viewer.ad_panel``."""
    channels: tuple[ChannelExports, ...]
    monetized: frozenset[str]
    """The channels with monetized playback configurations (``modules.release.channels``): they play on tickets."""
    personas: tuple[Persona, ...]
    """The demo personas; only their public fields are published, never a tier."""

    def document(self) -> dict[str, Any]:
        """The JSON document."""
        return {
            "region": self.region,
            "userPoolClientId": self.user_pool_client_id,
            "playbackUrl": self.playback_url,
            "cdnBaseUrl": self.cdn_base_url,
            "epgBaseUrl": f"{self.cdn_base_url}/epg",
            "planBaseUrl": f"{self.cdn_base_url}/{posixpath.dirname(plan_key('channel'))}",
            "adPanelUrl": self.ad_panel_url,
            "channels": [
                {
                    "name": channel.name,
                    "monetized": channel.name in self.monetized,
                    "playback": channel.playback(self.cdn_base_url),
                }
                for channel in self.channels
            ],
            "personas": [
                {
                    "id": persona.id,
                    "name": persona.name,
                    "bio": persona.bio,
                    "email": persona.email,
                    "signIn": persona.sign_in.value,
                    "account": persona.account,
                    "genres": list(persona.genres),
                }
                for persona in self.personas
            ],
        }


class ViewerSite(StaticSite):
    """The viewer's :class:`~fast_channels.infra.constructs.static_site.StaticSite`, on an origin of its own: viewer
    tokens never share an origin with operator tokens.

    Args:
        connect_sources: what the app calls besides Cognito: the media CDN and the APIs' origins.
        dist: the viewer build (``console/dist-viewer``).
    """

    def __init__(self, scope: Construct, id: str, *, connect_sources: Sequence[str], dist: Path = VIEWER_DIST) -> None:
        stack = Stack.of(scope)
        csp = viewer_csp(region=stack.region, url_suffix=stack.url_suffix, connect_sources=connect_sources)
        super().__init__(scope, id, csp=csp, dist=dist, text=TEXT)

    def publish(self, config: ViewerConfig) -> None:
        """Deploy the build and ``config.json``, then invalidate the entry objects."""
        self.publish_document(config.document())
