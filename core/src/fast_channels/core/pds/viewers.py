"""Viewer profiles: each signed-in viewer's ad-load tier, the one per-viewer input of ``/select`` (ADR-0037).

A profile is the item ``VIEWER#<vid>`` / ``PROFILE`` of the PDS table, keyed by the viewer id the binding resolves
(a keyed hash of the identity provider's subject, ``fast_channels.core.identity``), never by the subject itself. It
holds the persona and the tier. Only the PDS reads it: the tier never travels on the ticket, the wire, MediaTailor's
session state or a header (DESIGN §16.1, wire invariance). In the demo the seed script writes it from the persona list
(``fast_channels.functions.pds.profiles.put_profile``); a learned model would write the same item.

The pure part lives here; the reads and writes are ``fast_channels.functions.pds.profiles``.
"""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from fast_channels.core.adload import TIER_PATTERN
from fast_channels.core.personas import persona

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fast_channels.core.pds.delivery import Key

VID_PATTERN: Final = r"^[0-9a-f]{32}$"
"""A viewer id: 128 bits of the binding's keyed hash, in lowercase hex."""
PROFILE_PREFIX: Final = "VIEWER#"
"""Partition key prefix of the profiles (the sidecar's read grant names it)."""
PROFILE_SORT: Final = "PROFILE"
_VID: Final = re.compile(VID_PATTERN)
_TIER: Final = re.compile(TIER_PATTERN)


def profile_key(vid: str) -> Key:
    """The key of ``vid``'s profile.

    Raises:
        ValueError: ``vid`` is not 32 lowercase hex characters.
    """
    if not _VID.fullmatch(vid):
        raise ValueError(f"vid must be 32 lowercase hex characters, got {vid!r}")
    return {"pk": f"{PROFILE_PREFIX}{vid}", "sk": PROFILE_SORT}


def profile_item(vid: str, persona_id: str, *, now: datetime) -> dict[str, str]:
    """The profile of the viewer ``vid`` who is the persona ``persona_id``: their tier from the persona list.

    Raises:
        ValueError: an invalid ``vid``, an unknown persona, or one without a tier (a kids profile, the guest).
    """
    tier = persona(persona_id).tier
    if tier is None:
        raise ValueError(f"persona {persona_id!r} has no profile")
    return {
        **profile_key(vid),
        "persona": persona_id,
        "tier": tier,
        "updated_at": now.astimezone(UTC).isoformat(timespec="seconds"),
    }


def tier_of(item: Mapping[str, Any] | None) -> str | None:
    """The tier of a profile item as DynamoDB's low-level API returns it; ``None`` without one or for one that is
    not a tier's name."""
    value = ((item or {}).get("tier") or {}).get("S")
    return value if isinstance(value, str) and _TIER.fullmatch(value) else None
