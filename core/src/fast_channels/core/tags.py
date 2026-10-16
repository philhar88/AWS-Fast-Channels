"""Ownership tags on every AWS resource (CONTRACTS §E): one spelling for the keys and the owner values.

``fast:app`` scopes a resource to a deployment and ``fast:owner`` says who manages it. The reaper and
``scripts/cleanup.py`` delete runtime resources by these two tags alone, so a typo in either orphans a resource or
reaps one that CloudFormation owns.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

TAG_APP: Final = "fast:app"
"""Deployment prefix that owns the resource."""
TAG_OWNER: Final = "fast:owner"
"""Who manages the resource: an :class:`Owner` value."""
TAG_LADDER: Final = "fast:ladder"
"""Ladder version (``<id>@<fingerprint>``) a template, VOD source or channel belongs to."""
TAG_CORRELATION: Final = "fast:correlation-id"
"""Ingest execution or agent session that created a runtime resource."""

_NAMESPACE: Final = "fast:"


class Owner(StrEnum):
    """Who manages a resource (the ``fast:owner`` tag, and ``owner`` of MediaLibrary items)."""

    IAC = "iac"
    """CloudFormation; the reaper never deletes these."""
    PIPELINE = "pipeline"
    """The ingest pipeline (VOD sources, kit job templates)."""
    AGENT = "agent"
    """Agent tools (channels, source locations, ladder job templates); named ``<prefix>-agent-*`` where possible."""


RUNTIME_OWNERS: Final = frozenset({Owner.PIPELINE, Owner.AGENT})
"""Owners whose resources CloudFormation does not track; the reaper deletes these on teardown."""


def tag_key(name: str) -> str:
    """Tag key of an attribute name: ``fast:`` plus ``name`` with ``_`` as ``-`` (``asset_id`` -> ``fast:asset-id``)."""
    return f"{_NAMESPACE}{name.replace('_', '-')}"


def resource_tags(
    prefix: str,
    owner: Owner,
    *,
    ladder_version: str | None = None,
    correlation_id: str | None = None,
    **extra: str,
) -> dict[str, str]:
    """Tags of a resource of deployment ``prefix`` managed by ``owner``.

    ``ladder_version`` and ``correlation_id`` are added when given; each ``extra`` keyword becomes a
    :func:`tag_key` tag (``asset="x"`` -> ``fast:asset=x``).
    """
    optional = {TAG_LADDER: ladder_version, TAG_CORRELATION: correlation_id}
    return {
        TAG_APP: prefix,
        TAG_OWNER: owner.value,
        **{key: value for key, value in optional.items() if value is not None},
        **{tag_key(name): value for name, value in extra.items()},
    }


def owned_by(tags: Mapping[str, str], prefix: str) -> bool:
    """Whether ``tags`` mark a resource of deployment ``prefix`` (exact ``fast:app`` match)."""
    return tags.get(TAG_APP) == prefix


def runtime_owned(tags: Mapping[str, str], prefix: str) -> bool:
    """Whether ``tags`` mark a runtime resource of ``prefix``: one the reaper deletes on teardown."""
    return owned_by(tags, prefix) and tags.get(TAG_OWNER) in RUNTIME_OWNERS
