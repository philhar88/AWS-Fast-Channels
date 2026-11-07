"""``FastChannels-<prefix>-Viewer``: the reference viewer app (``modules.viewer``)."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_ssm as ssm

from fast_channels.core.identity import names as identity_names
from fast_channels.core.identity.names import Parameter
from fast_channels.core.personas import PERSONAS, Persona
from fast_channels.infra.constructs.viewer import VIEWER_DIST, AdPanelApi, ViewerConfig, ViewerSite
from fast_channels.infra.constructs.viewer.site import origin
from fast_channels.infra.stacks.recs import rail_url_parameter

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import MediaExports


class ViewerStack(Stack):
    """The viewer site, its ``config.json`` and, with ``modules.viewer.ad_panel``, the demo's ad panel API.

    The viewer pool, its client and the playback API are the identity module's. The stack reads them from the identity
    module's SSM parameters (``/<prefix>/identity/*``) at deploy time, as every consumer of that module does, so the
    Identity template does not change for it. It names them in ``config.json`` and the CSP. Monetized channels
    (``modules.release.channels``) play on tickets, the others on their v3.0.0 URLs.

    Args:
        config: deployment configuration (``modules.viewer``, ``modules.release``, ``observability``).
        media: Media stack exports (the CDN and the IaC channels).
        personas: the demo personas (``contracts/personas.json``); only their public fields are published.
        dist: the viewer build (``console/dist-viewer``).

    Attributes:
        site: the viewer site; ad_panel: the panel API, or ``None``.
        channels: the channels viewers see: the IaC channels, without the release module's lab channel.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        media: MediaExports,
        personas: Sequence[Persona] = PERSONAS,
        dist: Path = VIEWER_DIST,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): reference viewer", **kwargs)
        prefix = config.prefix

        def identity(parameter: identity_names.Parameter) -> str:
            return ssm.StringParameter.value_for_string_parameter(
                self, identity_names.parameter_name(prefix, parameter)
            )

        client_id, playback_url = identity(Parameter.CLIENT_ID), identity(Parameter.PLAYBACK_URL)
        lab = config.lab_channel
        self.channels = tuple(c for c in media.channels if lab is None or c.name != lab.name)
        self.ad_panel: AdPanelApi | None = None
        if config.modules.viewer.ad_panel:
            self.ad_panel = AdPanelApi(
                self,
                "AdPanel",
                prefix=prefix,
                issuer=identity(Parameter.ISSUER),
                client_id=client_id,
                vid_key_arn=identity(Parameter.VID_KEY),
                log_retention=config.observability.log_retention,
            )
        self.rail_url: str | None = None
        if config.modules.recs.enabled:
            self.rail_url = ssm.StringParameter.value_for_string_parameter(self, rail_url_parameter(prefix))
        # The CSP names each API's origin; the APIs answer CORS for any origin, so none refers to the site.
        apis = [
            playback_url,
            *([self.ad_panel.url] if self.ad_panel else []),
            *([self.rail_url] if self.rail_url else []),
        ]
        media_urls = [media.cdn_base_url, media.content_base_url]
        self.site = ViewerSite(self, "Site", connect_sources=[*media_urls, *map(origin, apis)], dist=dist)
        self.site.publish(
            ViewerConfig(
                region=self.region,
                user_pool_client_id=client_id,
                playback_url=playback_url,
                cdn_base_url=media.cdn_base_url,
                ad_panel_url=self.ad_panel.url if self.ad_panel else None,
                channels=self.channels,
                monetized=frozenset(config.release_channels),
                personas=tuple(personas),
                rail_url=self.rail_url,
            )
        )
        CfnOutput(self, "ViewerUrl", value=self.site.url, description="The reference viewer")
        if self.ad_panel:
            CfnOutput(self, "AdPanelUrl", value=self.ad_panel.url, description="The demo's ad panel API")
