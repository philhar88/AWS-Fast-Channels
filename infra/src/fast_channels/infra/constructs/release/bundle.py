"""The release bundle rendered at synth (DESIGN §8.1): CDK renders, the Release Controller applies.

:func:`render` builds the bundle from the configuration with :func:`fast_channels.core.monetization.bundle.build_bundle`
and validates it (synth fails on any violation). Values only known at deploy time (the channels' Channel Assembly
URLs, the CDN) are CDK tokens: each is swapped for a placeholder host while the bundle is built and validated, and put
back in its JSON, which CloudFormation then resolves. The ``Custom::DesiredState`` resource validates the resolved
bundle again, stores it under its digest and announces it.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fast_channels.core.monetization.bundle import (
    Bundle,
    BundleContribution,
    ChannelInput,
    RolloutTier,
    build_bundle,
    bundle_document,
)
from fast_channels.core.monetization.validators import require_valid, validate_bundle
from fast_channels.core.pbs_config import POD_SECONDS, stored_request_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fast_channels.infra.config import FastChannelsConfig

_TOKEN: Final = re.compile(r"\$\{Token\[[^\]]*\]\}")
STORED_REQUESTS: Final = tuple(stored_request_id(s, kids=kids) for kids in (False, True) for s in POD_SECONDS)
"""What the Prebid Server configuration serves (``core.pbs_config.render``): validator R-P5 checks the bodies."""


@dataclass(frozen=True, slots=True)
class RenderedBundle:
    """The bundle as built at synth (placeholders for deploy-time values) and its JSON with the tokens back."""

    bundle: Bundle
    document: str


class _Placeholders:
    def __init__(self) -> None:
        self.tokens: dict[str, str] = {}

    def hide(self, value: str) -> str:
        """``value`` with every CDK token replaced by a unique ``https://cdk-token-<n>.invalid`` placeholder host
        (the token is a whole URL) or ``cdk-token-<n>.invalid`` (part of one)."""

        def placeholder(match: re.Match[str]) -> str:
            whole = match.group() == value
            host = f"cdk-token-{len(self.tokens)}.invalid"
            text = f"https://{host}" if whole else host
            self.tokens[text] = match.group()
            return text

        return _TOKEN.sub(placeholder, value)

    def restore(self, text: str) -> str:
        for placeholder, token in sorted(self.tokens.items(), key=lambda item: -len(item[0])):
            text = text.replace(json.dumps(placeholder)[1:-1], token)
        return text


def render(
    config: FastChannelsConfig,
    *,
    pbs_origin: str,
    content_base_url: str,
    channel_urls: Mapping[str, str],
    contributions: Sequence[BundleContribution] = (),
) -> RenderedBundle:
    """The deployment's bundle: its monetized channels (``modules.release.channels``), the Prebid Server origin, and
    the module's contributions.

    Raises:
        InvalidBundleError: the bundle breaks a validator.
    """
    placeholders = _Placeholders()
    iac = {channel.name: channel for channel in config.channels.iac}
    channels = [
        ChannelInput(
            name=name,
            video_content_source_url=placeholders.hide(channel_urls[name]),
            outputs=iac[name].outputs,
        )
        for name in config.modules.release.channels
    ]
    bundle = build_bundle(
        prefix=config.prefix,
        channels=channels,
        content_base_url=placeholders.hide(content_base_url),
        pbs_origin=pbs_origin,
        log_percent=config.observability.mediatailor_log_percent,
        stored_requests=STORED_REQUESTS,
        grid_seconds=POD_SECONDS,
        tier=RolloutTier(config.modules.release.strategy.value),
        probe_sibling=config.modules.release.probe_sibling,
        contributions=contributions,
    )
    require_valid(validate_bundle(bundle))
    document = json.dumps(bundle_document(bundle)["bundle"], sort_keys=True, separators=(",", ":"))
    return RenderedBundle(bundle=bundle, document=placeholders.restore(document))
