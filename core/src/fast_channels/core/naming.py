"""Resource naming rules shared by infrastructure, pipeline and agent tools.

Every name is deterministic: the same inputs always give the same name, and names that would exceed a
service limit are shortened with a short content hash so they stay unique.
"""

import hashlib
import re
import unicodedata
from typing import Final
from urllib.parse import urlsplit
from warnings import deprecated

DEFAULT_PREFIX: Final = "fast"
"""Prefix of the default deployment, the one whose stack names carry no prefix (:func:`stage_id`)."""
AGENT_PREFIX_SUFFIX: Final = "agent"
"""Agent-owned runtime resources are named ``<prefix>-agent-...`` (Cedar forbids destructive tools elsewhere)."""
SOURCE_LOCATION_SUFFIX: Final = "media"
"""The IaC Channel Assembly source location of the media bucket is ``<prefix>-media``."""

MEDIATAILOR_NAME_MAX: Final = 64  # PlaybackConfiguration Name: ^[a-zA-Z0-9_-]+$, 1-64 (ad-insertion.md §1)
VOD_SOURCE_NAME_MAX: Final = 50  # VodSourceName: at most 50 (DescribeVodSource BadRequestException)
AGENTCORE_NAME_MAX: Final = 48  # Runtime/Memory names: [a-zA-Z][a-zA-Z0-9_]{0,47} (agentcore.md §2)
_HASH_LEN: Final = 6


def digest(text: str, length: int = _HASH_LEN) -> str:
    """Leading ``length`` hex characters of the SHA-256 of ``text``."""
    return hashlib.sha256(text.encode()).hexdigest()[:length]


def slug(text: str, max_len: int = 48) -> str:
    """Lower-case ASCII ``a-z0-9-`` slug of ``text`` (accents folded), at most ``max_len`` characters.

    Returns an empty string when ``text`` has no letters or digits.
    """
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:max_len].rstrip("-")


def _fit(name: str, limit: int, sep: str) -> str:
    if not name:
        raise ValueError("name has no valid characters")
    if len(name) <= limit:
        return name
    return f"{name[: limit - _HASH_LEN - 1].rstrip(sep)}{sep}{digest(name)}"


def mediatailor_name(*parts: str) -> str:
    """MediaTailor/Channel Assembly resource name: parts joined by ``-``, ``[A-Za-z0-9_-]``, at most 64 chars."""
    joined = "-".join(part for part in parts if part)
    return _fit(re.sub(r"[^A-Za-z0-9_-]+", "-", joined).strip("-"), MEDIATAILOR_NAME_MAX, "-")


def agentcore_name(*parts: str) -> str:
    """AgentCore resource name: parts joined by ``_``, alphanumerics and ``_`` only, letter first, at most 48 chars."""
    name = re.sub(r"[^A-Za-z0-9]+", "_", "_".join(part for part in parts if part)).strip("_")
    if not name[:1].isalpha():
        raise ValueError(f"AgentCore names must start with a letter: {name!r}")
    return _fit(name, AGENTCORE_NAME_MAX, "_")


def stage_id(prefix: str) -> str:
    """CDK stage id of deployment ``prefix``, which prefixes every stack name: ``FastChannels``
    (``FastChannels-Data``, ...) for :data:`DEFAULT_PREFIX`, else ``FastChannels-<prefix>``."""
    return "FastChannels" if prefix == DEFAULT_PREFIX else f"FastChannels-{prefix}"


def agent_namespace(prefix: str) -> str:
    """Name prefix of every agent-owned runtime resource of deployment ``prefix``: ``<prefix>-agent-``."""
    return f"{prefix}-{AGENT_PREFIX_SUFFIX}-"


def agent_owned_name(prefix: str, *parts: str) -> str:
    """Name of a runtime resource owned by the agent: ``<prefix>-agent-<parts...>``."""
    return mediatailor_name(prefix, AGENT_PREFIX_SUFFIX, *parts)


def is_agent_owned(name: str, prefix: str) -> bool:
    """Whether ``name`` belongs to the agent-owned namespace of deployment ``prefix``."""
    return name.startswith(agent_namespace(prefix))


def mediatailor_log_group(prefix: str) -> str:
    """The stage's MediaTailor vended-log group: every playback configuration's ad decision server, manifest and
    transcode logs."""
    return f"/aws/vendedlogs/mediatailor/{prefix}"


def ladder_path(ladder_version: str) -> str:
    """S3-key and name-safe form of a ladder version (``standard@0123abcd45`` -> ``standard_0123abcd45``).

    A string for paths and resource names; the MediaLibrary item key of a ladder is a different thing
    (``ladder_key`` in the functions package's library schema).
    """
    return ladder_version.replace("@", "_")


@deprecated("fast_channels.core.naming.ladder_key is renamed ladder_path")
def ladder_key(ladder_version: str) -> str:
    """Deprecated alias of :func:`ladder_path`, kept for v3.0 imports; removed in the next major version."""
    return ladder_path(ladder_version)


def job_template_name(prefix: str, ladder_version: str) -> str:
    """MediaConvert job template of a ladder version: ``<prefix>-<ladder id>_<fingerprint>``.

    The single source for CloudFormation (built-in ladders) and the agent's ``provision_ladder`` (custom ones),
    so the pipeline finds either by the ladder version alone.
    """
    return f"{prefix}-{ladder_path(ladder_version)}"


def source_location_name(prefix: str) -> str:
    """Channel Assembly source location of deployment ``prefix``'s media bucket: ``<prefix>-media``."""
    return mediatailor_name(prefix, SOURCE_LOCATION_SUFFIX)


def vod_source_name(asset_id: str, ladder_version: str) -> str:
    """Channel Assembly VOD source of an asset encode: unique per asset and ladder within its source location.

    VOD source names allow at most :data:`VOD_SOURCE_NAME_MAX` characters (``DescribeVodSource``: "The length cannot
    exceed 50"); a longer one is cut and ends in a hash of the whole name.
    """
    return _fit(mediatailor_name(asset_id, ladder_path(ladder_version)), VOD_SOURCE_NAME_MAX, "-")


def agent_vod_source_name(prefix: str, asset_id: str, ladder_version: str) -> str:
    """VOD source of content the agent registers: ``<prefix>-agent-<asset>-<fingerprint>``, at most
    :data:`VOD_SOURCE_NAME_MAX` characters (the agent namespace is kept)."""
    return _fit(agent_owned_name(prefix, asset_id, ladder_version.partition("@")[2]), VOD_SOURCE_NAME_MAX, "-")


def host_hash(url: str) -> str:
    """Short hash of a URL's host; versions artefacts that embed an absolute CDN host."""
    return digest(urlsplit(url).netloc.lower())


def creative_id(creative: str, ladder_version: str, content_base_url: str) -> str:
    """Immutable ad creative id ``<creative>-<ladder>-<hosthash>``.

    MediaTailor caches preconditioned imports by creative id, so the id must change whenever the media or
    the absolute host baked into the ad playlists changes (e2e.md §8 item 10).
    """
    return mediatailor_name(creative, ladder_path(ladder_version), host_hash(content_base_url))
