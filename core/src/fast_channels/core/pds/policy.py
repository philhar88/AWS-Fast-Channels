"""``/policy``: the privacy mode of a break and the patch that enforces it.

The PDS is the authority: whatever the session claims, each break's mode comes from the binding and the consent store,
in this order (docs/pds.md#privacy-modes):

1. a kids configuration or child-directed channel is KIDS (COPPA set, even for a probe), a probe session SAFE
   (house only);
2. a ticket that is missing, malformed, unknown, replayed, minted for another configuration or past its lease is
   CONTEXTUAL, with every viewer parameter zeroed;
3. a bound viewer whose consent cannot be read (store failure, or an answer older than 60 s) is SAFE: own book and
   house only, no third-party fan-out;
4. otherwise the consent decides: FULL, PSEUDONYMOUS, or CONTEXTUAL for an opt-out.

Prebid Server's own Activity Controls do the per-field disclosure for each mode; the patch only names bidders, sets
``regs`` and ``device`` (the viewer-IP rule and the device-type map), carries user data in FULL, and adds what patch
contributors supply. :func:`decide` runs the whole chain.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Final

from fast_channels.core.pds.contributors import Contributions, PatchContributor, collect
from fast_channels.core.pds.devicetype import device_type
from fast_channels.core.pds.models import OWN_BOOK, Device, Fpd, Mode, PolicyResponse, Reason, Regs
from fast_channels.core.pds.xff import IpRule, viewer_ip

if TYPE_CHECKING:
    from pydantic import JsonValue

    from fast_channels.core.pds.models import DeviceType, PolicyRequest
    from fast_channels.core.pds.xff import IpAddress, IpNetwork

TICKET: Final = re.compile(r"[A-Za-z0-9_-]{22}")
"""A playback ticket: ``secrets.token_urlsafe(16)``."""
KIDS_SIBLINGS: Final = frozenset({"k", "l"})
"""Sibling letters of the kids playback configurations ``<prefix>-<channel>-p-k`` and ``-p-l``."""


class Consent(StrEnum):
    """What the consent store says about a bound viewer."""

    FULL = "FULL"
    PSEUDONYMOUS = "PSEUDONYMOUS"
    OPTED_OUT = "OPTED_OUT"
    UNAVAILABLE = "UNAVAILABLE"
    """The store failed, or its answer is older than 60 s."""


@dataclass(frozen=True, slots=True)
class Viewer:
    """The viewer parameters of a binding. :data:`ANONYMOUS` is every one zeroed."""

    vid: str | None = None
    cid: str | None = None
    arm: str | None = None
    tier: int | None = None
    gpp: str | None = None
    gpp_sid: tuple[int, ...] = ()


ANONYMOUS: Final = Viewer()


@dataclass(frozen=True, slots=True)
class Resolution:
    """A break's mode, why, and the viewer parameters it may use."""

    mode: Mode
    reason: Reason
    viewer: Viewer = ANONYMOUS


def ticket_status(ticket: str) -> Reason | None:
    """``missing`` or ``invalid`` from the ticket's shape alone; ``None`` for a well-formed ticket to look up."""
    if not ticket:
        return Reason.MISSING
    return None if TICKET.fullmatch(ticket) else Reason.INVALID


def is_kids_configuration(pc: str) -> bool:
    """Whether ``pc`` is a kids sibling (``…-p-k`` or ``…-p-l``)."""
    head, sep, sibling = pc.rpartition("-p-")
    return bool(head and sep) and sibling in KIDS_SIBLINGS


def resolve(
    identity: Reason,
    *,
    viewer: Viewer = ANONYMOUS,
    consent: Consent | None = None,
    probe: bool = False,
    kids: bool = False,
) -> Resolution:
    """The mode of one break.

    Args:
        identity: the binding check's outcome, :attr:`Reason.BOUND` when the ticket is bound to this session and
            configuration, else the failure (``missing``, ``invalid``, ``unknown``, ``replay``, ``pc_mismatch``,
            ``expired_or_raced``).
        viewer: the binding row's viewer parameters, used only when ``identity`` is ``bound``.
        consent: the consent store's answer for a bound viewer.
        probe: a probe session.
        kids: a kids configuration or a child-directed channel.
    """
    if kids:
        return Resolution(Mode.KIDS, Reason.KIDS)
    if probe:
        return Resolution(Mode.SAFE, Reason.PROBE)
    if identity is not Reason.BOUND:
        return Resolution(Mode.CONTEXTUAL, identity)
    return _by_consent(consent, viewer)


def _by_consent(consent: Consent | None, viewer: Viewer) -> Resolution:
    match consent:
        case Consent.FULL:
            return Resolution(Mode.FULL, Reason.BOUND, viewer)
        case Consent.PSEUDONYMOUS:
            return Resolution(Mode.PSEUDONYMOUS, Reason.BOUND, viewer)
        case Consent.OPTED_OUT:  # no ids or user data; the GPP string still tells PBS what the viewer chose
            return Resolution(Mode.CONTEXTUAL, Reason.OPTED_OUT, Viewer(gpp=viewer.gpp, gpp_sid=viewer.gpp_sid))
        case _:
            return Resolution(Mode.SAFE, Reason.CONSENT_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class Demand:
    """The stage's demand allow-list: bidder → Prebid Server parameters, minus bidders a runbook shed."""

    bidders: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    shed: frozenset[str] = frozenset()

    def allowed(self) -> dict[str, dict[str, JsonValue]]:
        return {name: dict(params) for name, params in self.bidders.items() if name not in self.shed}


def policy_response(
    request: PolicyRequest,
    resolution: Resolution,
    demand: Demand,
    *,
    ip: IpAddress | None,
    devicetype: DeviceType | None,
    app_content: Mapping[str, JsonValue] | None = None,
    fpd_bidders: Sequence[str] = (OWN_BOOK,),
) -> PolicyResponse:
    """The ``/policy`` patch for ``request`` under ``resolution``.

    SAFE and KIDS add no bidder; ``regs.coppa`` is 1 in KIDS; GPP travels where the binding knows it; ``user`` and
    deals are FULL-only and therefore always empty before identity exists. ``ip`` is the viewer-IP rule's address
    (``None`` makes the module remove the header-derived one).
    """
    third_party = resolution.mode not in {Mode.SAFE, Mode.KIDS}
    viewer = resolution.viewer
    return PolicyResponse(
        decision_id=request.decision_id,
        mode=resolution.mode,
        reason=resolution.reason,
        bidders=demand.allowed() if third_party else {},
        regs=Regs(
            coppa=1 if resolution.mode is Mode.KIDS else 0,
            gpp=viewer.gpp,
            gpp_sid=list(viewer.gpp_sid) if viewer.gpp_sid else None,
        ),
        device=Device(
            ip=ip if isinstance(ip, IPv4Address) else None,
            ipv6=None if ip is None or isinstance(ip, IPv4Address) else ip,
            devicetype=devicetype,
        ),
        app_content=dict(app_content) if app_content is not None else None,
        fpd=Fpd(bidders=list(fpd_bidders)),
    )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One ``/policy`` answer and what its decision record needs besides it."""

    response: PolicyResponse
    ip_rule: IpRule
    contributions: Contributions


def decide(
    request: PolicyRequest,
    demand: Demand,
    *,
    origin_facing: Sequence[IpNetwork],
    identity: Reason | None = None,
    viewer: Viewer = ANONYMOUS,
    consent: Consent | None = None,
    kids_channel: bool = False,
    contributors: Sequence[PatchContributor] = (),
) -> PolicyDecision:
    """The ``/policy`` answer for ``request``.

    Args:
        request: the module's request.
        demand: the stage's bidder allow-list.
        origin_facing: CloudFront's origin-facing networks, for the viewer-IP rule.
        identity: the binding check's outcome; ``None`` takes it from the ticket's shape, so a well-formed ticket
            nobody looked up is ``unknown``.
        viewer: the binding's viewer parameters (with ``identity`` ``bound``).
        consent: the consent store's answer for a bound viewer.
        kids_channel: the calling configuration's channel is child-directed.
        contributors: sources of further patch fields, asked in order.
    """
    if identity is None:
        identity = ticket_status(request.t) or Reason.UNKNOWN
    resolution = resolve(
        identity,
        viewer=viewer,
        consent=consent,
        probe=request.probe,
        kids=kids_channel or is_kids_configuration(request.pc),
    )
    address = viewer_ip(request.xff, request.cfva, origin_facing)
    contributions = collect(contributors, request, resolution.mode)
    response = policy_response(
        request,
        resolution,
        demand,
        ip=address.ip,
        devicetype=device_type(request.ua),
        app_content=contributions.app_content or None,
    )
    return PolicyDecision(response, address.rule, contributions)
