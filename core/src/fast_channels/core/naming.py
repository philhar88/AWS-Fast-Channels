"""Resource naming rules shared by infrastructure, pipeline and agent tools.

Every name is deterministic: the same inputs always give the same name, and names that would exceed a
service limit are shortened with a short content hash so they stay unique.
"""

import hashlib
import re
import unicodedata
from typing import Final
from urllib.parse import urlsplit

AGENT_PREFIX_SUFFIX: Final = "agent"
"""Agent-owned runtime resources are named ``<prefix>-agent-...`` (Cedar forbids destructive tools elsewhere)."""

MEDIATAILOR_NAME_MAX: Final = 64  # PlaybackConfiguration Name: ^[a-zA-Z0-9_-]+$, 1-64 (ad-insertion.md §1)
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


def agent_owned_name(prefix: str, *parts: str) -> str:
    """Name of a runtime resource owned by the agent: ``<prefix>-agent-<parts...>``."""
    return mediatailor_name(prefix, AGENT_PREFIX_SUFFIX, *parts)


def is_agent_owned(name: str, prefix: str) -> bool:
    """Whether ``name`` belongs to the agent-owned namespace of deployment ``prefix``."""
    return name.startswith(f"{prefix}-{AGENT_PREFIX_SUFFIX}-")


def ladder_key(ladder_version: str) -> str:
    """S3-key and path-safe form of a ladder version (``standard@0123abcd45`` -> ``standard_0123abcd45``)."""
    return ladder_version.replace("@", "_")


def host_hash(url: str) -> str:
    """Short hash of a URL's host; versions artefacts that embed an absolute CDN host."""
    return digest(urlsplit(url).netloc.lower())


def creative_id(creative: str, ladder_version: str, content_base_url: str) -> str:
    """Immutable ad creative id ``<creative>-<ladder>-<hosthash>``.

    MediaTailor caches preconditioned imports by creative id, so the id must change whenever the media or
    the absolute host baked into the ad playlists changes (e2e.md §8 item 10).
    """
    return mediatailor_name(creative, ladder_key(ladder_version), host_hash(content_base_url))
