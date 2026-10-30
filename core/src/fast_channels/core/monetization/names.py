"""Names of the release module's resources, shared by the ``Release`` stack (``infra``), the Release Controller's
functions and the deploy scripts, so every side agrees.

The ``Release`` stack deploys beside the Prebid Server guidance (after the ``Pds`` stack), because its bundle names
the guidance's CloudFront domain. It reads what the ``Media`` stack knows through the SSM parameters below, never
through a cross-stack reference: the Media stack also deploys without it (``make deploy``).
"""

from enum import StrEnum
from typing import Final

from fast_channels.core.tags import tag_key

MODULE: Final = "release"
MODULE_TAG: Final = tag_key("module")
PRINCIPAL: Final = "release-controller"
"""The machine principal of every Release Controller ledger record (ADR-0023)."""
PLAN_CACHE_CONTROL: Final = "max-age=30"
"""``/plan/playback/<channel>.json`` changes at every promotion and rollback (DESIGN §8.2)."""
HISTORY: Final = 3
"""Trees kept per sibling: the current one, the last good one and the one before (DESIGN §8.5)."""


class Parameter(StrEnum):
    """SSM parameters ``/<prefix>/release/<name>`` the Media stack writes when the release module is on."""

    CONTENT_BASE_URL = "content-base-url"
    """Base URL of content and ad segments (the siblings' ``CdnConfiguration``)."""
    CDN_BASE_URL = "cdn-base-url"
    """The deployment's CloudFront distribution (the playback URLs in ``/plan/playback``)."""
    MEDIA_BUCKET = "media-bucket"
    """Where ``plan/playback/<channel>.json`` is written (served by the CDN's default route)."""


def parameter_name(prefix: str, parameter: Parameter) -> str:
    """``/<prefix>/release/<parameter>``."""
    return f"/{prefix}/release/{parameter.value}"


def channel_parameter_name(prefix: str, channel: str) -> str:
    """``/<prefix>/release/channels/<channel>``: the channel's Channel Assembly base URL (``VideoContentSourceUrl``)."""
    return f"/{prefix}/release/channels/{channel}"


def stack_name(stage_id: str) -> str:
    """The ``Release`` stack of a stage (``FastChannels-<prefix>``)."""
    return f"{stage_id}-Release"


def state_table(prefix: str) -> str:
    """The Release Controller's state: releases, and each monetized channel's siblings."""
    return f"{prefix}-release"


def state_machine(prefix: str) -> str:
    """The apply path (Step Functions Standard)."""
    return f"{prefix}-release"


def log_group(prefix: str) -> str:
    """The siblings' MediaTailor vended logs, encrypted with the module's own KMS key (DESIGN §5.7)."""
    return f"/aws/vendedlogs/mediatailor/{prefix}-release"


def log_destination(prefix: str) -> str:
    """The delivery destination of :func:`log_group`."""
    return f"{prefix}-release-mediatailor"


def lake_destinations(prefix: str) -> tuple[str, str]:
    """The ops module's lake delivery destinations of the ad decision server and the manifest logs (``Lake``)."""
    return f"{prefix}-lake-ads", f"{prefix}-lake-manifest"


def bundle_key(digest: str) -> str:
    """S3 key of a bundle document in the release bucket."""
    return f"release/{digest}/bundle.json"


def plan_key(channel: str) -> str:
    """S3 key (in the media bucket) of the channel's playback plan, the stable sibling's playback URLs."""
    return f"plan/playback/{channel}.json"


def channel_chain(channel: str) -> str:
    """Ledger chain of a channel's releases."""
    return f"release/{channel}"


BUNDLE_CHAIN: Final = "release/bundle"
"""Ledger chain of bundle validations (accepted or rejected)."""


def function_pattern(prefix: str) -> str:
    """Every FunctionId of the deployment: ``<p>_*`` (IAM resource pattern)."""
    return f"{prefix}_*"


def sibling_pattern(channel: str) -> str:
    """Every sibling configuration of a channel: ``<channel>-p-*`` (IAM resource pattern)."""
    return f"{channel}-p-*"
