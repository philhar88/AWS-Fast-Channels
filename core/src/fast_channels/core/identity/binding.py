"""The binding check: what a ticket's row says about one break of one session (DESIGN-v2.2 §5.4, ADR-0018).

The Pod Decision Service is the authority. On every break it reads ``T#<t>`` with a consistent read and runs
:func:`check`, which is pure: the row, the calling session and configuration, and the time decide.

- **Bound** only if ``redeemedBy`` is this session and ``pc`` this configuration, inside the lease: idle at most
  :data:`IDLE_MS` since ``last_seen``, and at most :data:`ABSOLUTE_MS` since ``bound_at``.
- **Self-redeem.** A row nobody redeemed is bound to this session if its redeem window is still open (the PRE_SESSION
  redeem failed or has not landed): :attr:`Action.SELF_REDEEM`, the same conditional update as the identity API's.
- **Refresh.** A bound row whose ``last_seen`` is older than :data:`REFRESH_MS` gets it renewed
  (:attr:`Action.REFRESH`), so a watching viewer stays inside the idle lease at one write per 10 minutes.
- Anything else is anonymous, with the reason: ``unknown`` (no row), ``pc_mismatch``, ``replay`` (bound to another
  session) or ``expired_or_raced`` (the window passed unredeemed, or the lease ended).

A bound signed-in viewer's mode then comes from their consent (``core.identity.consent``), which the caller reads
(:attr:`Binding.signed_in`); a bound guest's from the mode the guest mint put on the ticket.

The row's times are epoch milliseconds (API Gateway's ``$context.requestTimeEpoch`` and the mint's clock).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from fast_channels.core.identity.consent import UNKNOWN, StoredConsent
from fast_channels.core.identity.ticket import REDEEM_CONDITION, REDEEM_UPDATE, PrivacyMode, key
from fast_channels.core.pds.models import Reason
from fast_channels.core.pds.policy import ANONYMOUS, Consent, Viewer

IDLE_MS: Final = 2 * 3600 * 1000
"""A binding ends after 2 hours without a break (``last_seen``)."""
ABSOLUTE_MS: Final = 24 * 3600 * 1000
"""A binding ends 24 hours after it was made (``bound_at``), however long the session plays."""
REFRESH_MS: Final = 10 * 60 * 1000
"""``last_seen`` is renewed at most this often."""
REFRESH_CONDITION: Final = "redeemedBy = :sid"
REFRESH_UPDATE: Final = "SET last_seen = :now"


class Action(StrEnum):
    """The write a check asks for besides its answer."""

    NONE = "none"
    SELF_REDEEM = "self_redeem"
    """Bind the unredeemed row to this session (``REDEEM_CONDITION``); a failed condition means another session or
    the clock won, and the break is ``expired_or_raced``."""
    REFRESH = "refresh"
    """Renew ``last_seen`` (``REFRESH_CONDITION``); a failure changes nothing for this break."""


@dataclass(frozen=True, slots=True)
class Binding:
    """The outcome of one check: :attr:`reason` is ``bound`` or why not, with the row's viewer values when bound."""

    reason: Reason
    vid: str | None = None
    cid: str | None = None
    persona: str | None = None
    pm: PrivacyMode | None = None
    gpp: str | None = None
    gpp_sid: tuple[int, ...] = ()
    action: Action = Action.NONE

    @property
    def bound(self) -> bool:
        return self.reason is Reason.BOUND

    @property
    def signed_in(self) -> bool:
        """Bound to a signed-in viewer, whose consent decides the mode."""
        return self.bound and self.vid is not None


def _ms(row: Mapping[str, Any], name: str) -> int | None:
    value = row.get(name)
    return None if value is None else int(value)


def _bound(row: Mapping[str, Any], action: Action) -> Binding:
    mode = row.get("pm")
    return Binding(
        Reason.BOUND,
        vid=row.get("vid"),
        cid=row.get("cid"),
        persona=row.get("persona"),
        pm=PrivacyMode(mode) if mode in set(PrivacyMode) else None,  # an unknown mode: consent unavailable, SAFE
        gpp=row.get("gpp"),
        gpp_sid=tuple(int(section) for section in row.get("gpp_sid") or ()),
        action=action,
    )


def check(row: Mapping[str, Any] | None, *, sid: str, pc: str, now_ms: int) -> Binding:
    """The binding of ``row`` (deserialized, or None when there is none) for session ``sid`` of configuration ``pc``
    at ``now_ms`` (module docstring)."""
    if row is None:
        return Binding(Reason.UNKNOWN)
    if row.get("pc") != pc:
        return Binding(Reason.PC_MISMATCH)
    redeemed = row.get("redeemedBy")
    if redeemed is None:
        return _unredeemed(row, now_ms)
    if redeemed != sid:
        return Binding(Reason.REPLAY)
    return _leased(row, now_ms)


def _unredeemed(row: Mapping[str, Any], now_ms: int) -> Binding:
    """A row nobody bound: this session's while the redeem window is open."""
    redeem_by = _ms(row, "redeem_by")
    if redeem_by is not None and redeem_by > now_ms:
        return _bound(row, Action.SELF_REDEEM)
    return Binding(Reason.EXPIRED_OR_RACED)


def _leased(row: Mapping[str, Any], now_ms: int) -> Binding:
    """A row bound to this session: bound inside the lease, renewed when ``last_seen`` is due."""
    bound_at = _ms(row, "bound_at") or 0
    last_seen = _ms(row, "last_seen") or bound_at
    if now_ms - bound_at >= ABSOLUTE_MS or now_ms - last_seen >= IDLE_MS:
        return Binding(Reason.EXPIRED_OR_RACED)
    return _bound(row, Action.REFRESH if now_ms - last_seen >= REFRESH_MS else Action.NONE)


@dataclass(frozen=True, slots=True)
class PolicyInputs:
    """What ``core.pds.policy.decide`` takes from a binding."""

    identity: Reason
    viewer: Viewer = ANONYMOUS
    consent: Consent | None = None


GUEST_CONSENT: Final[Mapping[PrivacyMode, Consent]] = {
    PrivacyMode.CONTEXTUAL: Consent.CONTEXTUAL,
    PrivacyMode.OPTED_OUT: Consent.OPTED_OUT,
}
"""A guest ticket's mode as the consent the policy reads; any other mode on a ticket without a ``vid`` is SAFE."""
LOOKUP_FAILED: Final = PolicyInputs(Reason.BOUND, consent=Consent.UNAVAILABLE)
"""A well-formed ticket whose row could not be read: an identity failure for what may be a signed-in viewer, so the
break is SAFE (own book and house only), never CONTEXTUAL with third parties (DESIGN-v2.2 §5.5)."""


def policy_inputs(binding: Binding, consent: StoredConsent = UNKNOWN) -> PolicyInputs:
    """The policy's identity, viewer and consent from ``binding``: the row's viewer values only when bound. A signed-in
    viewer's consent is ``consent``, the store's answer (unread: SAFE); a guest's is the ticket's mode and GPP
    string."""
    if not binding.bound:
        return PolicyInputs(binding.reason)
    if binding.signed_in:
        viewer = Viewer(
            vid=binding.vid, cid=binding.cid, persona=binding.persona, gpp=consent.gpp, gpp_sid=consent.gpp_sid
        )
        return PolicyInputs(Reason.BOUND, viewer, consent.consent)
    guest = Viewer(cid=binding.cid, gpp=binding.gpp, gpp_sid=binding.gpp_sid)
    mode = GUEST_CONSENT.get(binding.pm) if binding.pm else None
    return PolicyInputs(Reason.BOUND, guest, mode or Consent.UNAVAILABLE)


def self_redeem(table: str, ticket: str, *, sid: str, pc: str, now_ms: int) -> dict[str, Any]:
    """The ``UpdateItem`` request (low-level client) of :attr:`Action.SELF_REDEEM`: the identity API's own."""
    return {
        "TableName": table,
        "Key": {"pk": {"S": key(ticket)}},
        "ConditionExpression": REDEEM_CONDITION,
        "UpdateExpression": REDEEM_UPDATE,
        "ExpressionAttributeValues": {":sid": {"S": sid}, ":pc": {"S": pc}, ":now": {"N": str(now_ms)}},
    }


def refresh(table: str, ticket: str, *, sid: str, now_ms: int) -> dict[str, Any]:
    """The ``UpdateItem`` request of :attr:`Action.REFRESH`."""
    return {
        "TableName": table,
        "Key": {"pk": {"S": key(ticket)}},
        "ConditionExpression": REFRESH_CONDITION,
        "UpdateExpression": REFRESH_UPDATE,
        "ExpressionAttributeValues": {":sid": {"S": sid}, ":now": {"N": str(now_ms)}},
    }
