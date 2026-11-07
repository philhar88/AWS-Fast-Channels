"""The consent store and the guests' rule (DESIGN-v2.2 §5.1, §5.5; ADR-0018).

A signed-in viewer's consent is one row ``C#<vid>`` of the identity table, which the viewer writes with
``PUT /consent`` (``functions.identity.mint``): whether they opted out of sale and sharing, whether they gave the
separate video-privacy (VPPA) consent, and the GPP string and its applicable sections where their consent platform
gives them. The strings are stored as given and travel to Prebid Server in ``regs``; its Activity Controls interpret
them, so no GPP decoder is written here.

The Pod Decision Service reads the row on every break of a bound viewer (``fast_channels.pds.bindings``) and maps it
with :func:`consent_of`: an opt-out is CONTEXTUAL, a VPPA consent FULL, anything else PSEUDONYMOUS; a missing row,
or one older than :data:`MAX_AGE` (the TCF's re-prompt after 13 months), is SAFE: an authenticated viewer whose
consent nobody knows gets no third-party fan-out.

A guest has no row. The guest mint (``POST /playback/guest``) records the guest's mode on the ticket with
:func:`guest_mode`: opted out with Global Privacy Control, contextual otherwise; the GPP string rides on the ticket.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.identity.ticket import GPP_PATTERN, GPP_SECTIONS_MAX, PrivacyMode
from fast_channels.core.pds.policy import Consent

KEY_PREFIX: Final = "C#"
MAX_AGE: Final = timedelta(days=395)
"""A consent older than this is stale: the TCF asks again after 13 months."""


def key(vid: str) -> str:
    """The consent row of ``vid``: ``C#<vid>``."""
    return f"{KEY_PREFIX}{vid}"


class ConsentChoice(BaseModel):
    """What a viewer chose: the body of ``PUT /consent``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    opt_out: bool = Field(description="Opted out of the sale and sharing of personal information.")
    vppa: bool = Field(default=False, description="Gave the separate video-privacy consent to share what they watch.")
    gpp: str | None = Field(default=None, pattern=GPP_PATTERN, description="The consent platform's GPP string.")
    gpp_sid: tuple[int, ...] = Field(default=(), max_length=GPP_SECTIONS_MAX, description="Applicable GPP sections.")


class ConsentRecord(ConsentChoice):
    """The row ``C#<vid>``: the choice and when it was made (epoch ms)."""

    pk: str = Field(pattern=r"^C#[0-9a-f]{32}$")
    updated_at: int = Field(ge=0)

    def item(self) -> dict[str, Any]:
        """The row as a DynamoDB resource item, without the fields that are unset."""
        item = self.model_dump(mode="json", exclude_none=True)
        if not self.gpp_sid:
            item.pop("gpp_sid")
        return item


def record(vid: str, choice: ConsentChoice, *, now_ms: int) -> ConsentRecord:
    """The row of ``vid``'s ``choice`` made at ``now_ms``."""
    return ConsentRecord(pk=key(vid), updated_at=now_ms, **choice.model_dump())


def consent_of(row: ConsentRecord | None, *, now: datetime) -> Consent:
    """The consent the policy reads from ``row`` at ``now`` (module docstring)."""
    if row is None or now.timestamp() * 1000 - row.updated_at > MAX_AGE.total_seconds() * 1000:
        return Consent.UNAVAILABLE
    if row.opt_out:
        return Consent.OPTED_OUT
    return Consent.FULL if row.vppa else Consent.PSEUDONYMOUS


@dataclass(frozen=True, slots=True)
class StoredConsent:
    """What the policy takes from a bound viewer's consent: the consent and the GPP string with its sections."""

    consent: Consent
    gpp: str | None = None
    gpp_sid: tuple[int, ...] = ()


UNKNOWN: Final = StoredConsent(Consent.UNAVAILABLE)
"""No answer from the store: SAFE."""


def stored(row: ConsentRecord | None, *, now: datetime) -> StoredConsent:
    """``row`` as the policy reads it at ``now``; a missing or stale row is :data:`UNKNOWN`."""
    consent = consent_of(row, now=now)
    if row is None or consent is Consent.UNAVAILABLE:
        return UNKNOWN
    return StoredConsent(consent, row.gpp, row.gpp_sid)


def guest_mode(*, gpc: bool) -> PrivacyMode:
    """A guest's mode: opted out with Global Privacy Control (``Sec-GPC: 1`` or ``gpc``), else contextual. Either way
    no identifier or user data reaches a third party (DESIGN-v2.2 §5.5); the GPP string tells Prebid Server the rest."""
    return PrivacyMode.OPTED_OUT if gpc else PrivacyMode.CONTEXTUAL
