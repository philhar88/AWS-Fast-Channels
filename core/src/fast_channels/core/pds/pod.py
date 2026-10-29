"""Pod selection: from every bid of one auction to the pod the module renders (docs/pds.md#pod-rules).

Deterministic and exact for the stored requests' equal 30 s slots:

1. **Eligibility.** Own book only in SAFE, KIDS, or when the module applied no policy, and in KIDS only house ads and
   creatives approved for children; house ads only for probes. A house ad is an own-book bid marked house: the flag
   from any other bidder is ignored. Usable VAST with a known duration; a declared ``dur`` must agree with it (rounded
   half up, as MediaTailor rounds ``avail_duration_secs``), and an absent one is taken from it; the slot must accept it
   (``rqddurs``). A ``crid``, and an ``adomain`` unless it is a house ad. No ``crid`` the session saw in its recent
   breaks. At or above the floor, except house ads (fillers) and tier-0 lines (contracted).
2. **Value.** Tier-0 lines first (sponsorships, guaranteed lines, PG deals: their ``dealid`` is in the tier-0 set),
   then paid bids by accepted net value ``price / 1000 * accept_ratio(bidder) - c_ins``, which must beat the slot's
   ad-load price λ (:mod:`fast_channels.core.adload`); house ads fill what is left where λ is 0.
3. **Placement.** A bid stays in its own imp's slot. Candidates are taken best first; one is placed if its slot is
   free, it repeats no ``crid``, ``adomain`` or IAB category already in the pod (across bidders; house ads, which
   advertise nothing, only repeat no ``crid``), and the pod still fits: Σ ``dur`` ≤ ``poddur`` and Σ VAST durations
   ≤ the break. Slots nothing fills are not returned; Channel Assembly's break slate plays them (MediaTailor keeps a
   partial pod at a personalization threshold of 121 s).
4. **Standalones.** Per winner, the best remaining candidate of the same imp and duration: what MediaTailor's ad
   buffet plays in that slot if the winner fails. The pod stays within the break even if every winner fails.
5. **Empty pod** for an avail under 30 s (the tail of a break a session joined late).

The break is ``dur_ms`` as MediaTailor requested it, except on prefetched breaks: a prefetch retrieval carries the
duration rounded to whole seconds (60,000 for a 60,060 ms break), so when ``dur_ms`` is exactly the schedule of
record's duration rounded, the schedule's duration is the limit (2 x 30.03 s fits 60.000).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Annotated, Final
from urllib.parse import quote, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.adload import slot_lambdas
from fast_channels.core.pds.models import (
    OWN_BOOK,
    Bid,
    Mode,
    PodAd,
    PodReason,
    SelectRequest,
    SelectResponse,
    Slot,
    StandaloneAd,
)
from fast_channels.core.timeline import whole_seconds

MIN_AVAIL_MS: Final = 30_000
"""Avails shorter than this get an empty pod."""
C_INS: Final = Decimal("0.0005")
"""``c_ins``: the cost of inserting one ad, US dollars."""
_OWN_BOOK_ONLY: Final = frozenset({Mode.SAFE, Mode.KIDS, None})


class Outcome(StrEnum):
    """What became of one bid (decision records; forgone-revenue accounting)."""

    WON = "won"
    STANDALONE = "standalone"
    SHORT_AVAIL = "short_avail"
    MODE = "mode"
    """Not the own book, in a mode that allows only the own book."""
    KIDS = "kids"
    """Neither a house ad nor a creative approved for children, on a kids break."""
    PROBE = "probe"
    """Not a house ad, on a probe session."""
    INVALID_VAST = "invalid_vast"
    NO_DURATION = "no_duration"
    DURATION_MISMATCH = "duration_mismatch"
    """The VAST duration, rounded to seconds, is not the declared ``dur``."""
    RQDDURS = "rqddurs"
    """The slot does not accept the ad's duration."""
    NO_CRID = "no_crid"
    NO_ADOMAIN = "no_adomain"
    RECENT = "recent_crid"
    """The session saw this creative in its recent breaks."""
    FLOOR = "below_floor"
    BAR = "below_bar"
    """Its net value does not beat the slot's ad-load price."""
    OUTBID = "outbid"
    """A better candidate took its slot."""
    SEPARATION = "separation"
    """Its creative, advertiser domain or IAB category is already in the pod."""
    NO_ROOM = "no_room"
    """The pod's durations are used up."""
    UNRESERVED = "unreserved"
    """An own-book line ad whose price could not be reserved: no budget left, the session's cap reached
    (``core.pds.selection``)."""


class PodContext(BaseModel):
    """What the PDS knows besides the request: its snapshot (lines, floors, sources) and the session's state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    own_book: frozenset[str] = Field(default=frozenset({OWN_BOOK}), description="Bidder names of the own book.")
    tier0_deals: frozenset[str] = Field(default=frozenset(), description="Deal ids of tier-0 lines and PG deals.")
    kids_approved: frozenset[str] = Field(default=frozenset(), description="Creatives approved for kids breaks.")
    accept_ratio: dict[str, Annotated[Decimal, Field(ge=0, le=1)]] = Field(
        default_factory=dict, description="Share of a bidder's ads MediaTailor plays, by bidder; 1 when absent."
    )
    floor_cpm: Decimal = Field(default=Decimal(0), ge=0, description="Price floor for paid bids, CPM.")
    recent_crids: frozenset[str] = Field(default=frozenset(), description="Creatives of the session's recent breaks.")
    scheduled_ms: int | None = Field(default=None, gt=0, description="Duration of the break in the schedule of record.")
    lambdas: tuple[Annotated[Decimal, Field(ge=0)], ...] = Field(
        default=(), description="Ad-load price per slot, US dollars; empty for the ad-load model's (slot_lambdas)."
    )


DEFAULT_CONTEXT: Final = PodContext()
"""No snapshot: the own book is ``ownbook``, no tier-0 deals, no floor, no session history."""


class _Kind(IntEnum):
    TIER0 = 0
    PAID = 1
    HOUSE = 2


@dataclass(frozen=True, slots=True)
class Placement:
    """A bid placed in the pod or as a standalone."""

    ref: str
    impid: str
    slot: int
    crid: str
    dur: int
    vast_dur_ms: int


@dataclass(frozen=True, slots=True)
class PodDecision:
    """The selected pod with every bid's outcome."""

    reason: PodReason
    limit_ms: int
    """The break's VAST-duration budget."""
    poddur: int
    pod: tuple[Placement, ...] = ()
    """Winners in slot order: ``sequence`` 1..n."""
    standalones: tuple[Placement, ...] = ()
    outcomes: Mapping[str, Outcome] = field(default_factory=dict)
    tier0: frozenset[str] = frozenset()
    """Refs of the bids from tier-0 lines and deals."""
    below_floor: frozenset[str] = frozenset()
    """Refs of the bids under the floor (house ads and tier-0 lines are never under it)."""

    @property
    def filled_ms(self) -> int:
        return sum(ad.vast_dur_ms for ad in self.pod)


@dataclass(frozen=True, slots=True)
class _Candidate:
    bid: Bid
    slot: int
    kind: _Kind
    net: Decimal
    crid: str
    dur: int
    vast_dur_ms: int
    claims: frozenset[str]

    def rank(self) -> tuple[int, Decimal, int, str]:
        return self.kind, -self.net, self.slot, self.bid.ref


def limit_ms(dur_ms: int, scheduled_ms: int | None) -> int:
    """The break's VAST-duration budget: ``dur_ms``, or the schedule's when ``dur_ms`` is its prefetch rounding.

    A live request carries the frame-exact duration (or, for a late join, the remainder), so a whole-second
    ``dur_ms`` equal to the schedule of record's rounded duration is a prefetch retrieval's.
    """
    return scheduled_ms if scheduled_ms is not None and _prefetched(dur_ms, scheduled_ms) else dur_ms


def _prefetched(dur_ms: int, scheduled_ms: int) -> bool:
    """Whether ``dur_ms`` is a prefetch retrieval's rounding of the ``scheduled_ms`` break."""
    return dur_ms != scheduled_ms and dur_ms == whole_seconds(scheduled_ms) * 1000


def scheduled_ms(dur_ms: int, breaks_ms: Iterable[int]) -> int | None:
    """The break of record (one of the schedule's ``breaks_ms``) that ``dur_ms`` is the prefetch rounding of (60,000
    for 60,060), or None."""
    return next((ms for ms in breaks_ms if _prefetched(dur_ms, ms)), None)


def _house(bid: Bid, context: PodContext) -> bool:
    return bid.house and bid.bidder in context.own_book


def _tier0(bid: Bid, context: PodContext) -> bool:
    return not _house(bid, context) and bid.dealid is not None and bid.dealid in context.tier0_deals


def _below_floor(bid: Bid, context: PodContext) -> bool:
    return not _house(bid, context) and not _tier0(bid, context) and bid.price < context.floor_cpm


def _dur(bid: Bid) -> int:
    """The ad's duration in whole seconds: declared, else its VAST duration rounded."""
    return bid.dur or whole_seconds(bid.vast_dur_ms or 0)


def _adomains(bid: Bid) -> frozenset[str]:
    return frozenset(domain.strip().lower() for domain in bid.adomain) - {""}


def _ineligible(bid: Bid, slot: Slot, request: SelectRequest, context: PodContext) -> Outcome | None:
    house = _house(bid, context)
    checks: tuple[tuple[bool, Outcome], ...] = (
        (request.mode in _OWN_BOOK_ONLY and bid.bidder not in context.own_book, Outcome.MODE),
        (request.mode is Mode.KIDS and not house and bid.crid not in context.kids_approved, Outcome.KIDS),
        (request.probe and not house, Outcome.PROBE),
        (bid.adm_kind is None, Outcome.INVALID_VAST),
        (not bid.vast_dur_ms, Outcome.NO_DURATION),
        (bool(bid.dur) and whole_seconds(bid.vast_dur_ms or 0) != bid.dur, Outcome.DURATION_MISMATCH),
        (bool(slot.rqddurs) and _dur(bid) not in slot.rqddurs, Outcome.RQDDURS),
        (not bid.crid, Outcome.NO_CRID),
        (not house and not _adomains(bid), Outcome.NO_ADOMAIN),
        (bid.crid in context.recent_crids, Outcome.RECENT),
        (_below_floor(bid, context), Outcome.FLOOR),
    )
    return next((outcome for failed, outcome in checks if failed), None)


def _claims(bid: Bid, house: bool) -> frozenset[str]:
    """What a placed bid reserves in the pod: its creative, and for a sold ad its advertiser domains and categories.

    House ads are the publisher's own fillers, not advertisers, so they take part in creative dedupe only.
    """
    if house:
        return frozenset({f"crid:{bid.crid}"})
    taxonomy = bid.cattax or 1
    categories = {category.strip() for category in bid.cat} - {""}
    return frozenset(
        [f"crid:{bid.crid}", *(f"adomain:{domain}" for domain in _adomains(bid))]
        + [f"cat:{taxonomy}:{category}" for category in categories]
    )


def _candidate(bid: Bid, slot: int, context: PodContext) -> _Candidate:
    house = _house(bid, context)
    if house:
        kind = _Kind.HOUSE
    elif _tier0(bid, context):
        kind = _Kind.TIER0
    else:
        kind = _Kind.PAID
    ratio = context.accept_ratio.get(bid.bidder, Decimal(1))
    return _Candidate(
        bid=bid,
        slot=slot,
        kind=kind,
        net=bid.price / 1000 * ratio - C_INS,
        crid=bid.crid or "",
        dur=_dur(bid),
        vast_dur_ms=bid.vast_dur_ms or 0,
        claims=_claims(bid, house),
    )


def _wanted(candidate: _Candidate, lambdas: tuple[Decimal, ...]) -> bool:
    match candidate.kind:
        case _Kind.TIER0:
            return True
        case _Kind.PAID:
            return candidate.net - lambdas[candidate.slot] > 0
        case _Kind.HOUSE:
            return lambdas[candidate.slot] <= 0  # a filler earns nothing: it runs only where ad load costs nothing


def _placement(candidate: _Candidate) -> Placement:
    return Placement(
        ref=candidate.bid.ref,
        impid=candidate.bid.impid,
        slot=candidate.slot,
        crid=candidate.crid,
        dur=candidate.dur,
        vast_dur_ms=candidate.vast_dur_ms,
    )


def _candidates(
    request: SelectRequest, context: PodContext, lambdas: tuple[Decimal, ...], outcomes: dict[str, Outcome]
) -> list[_Candidate]:
    """The eligible, wanted bids, best first; every other bid's outcome goes into ``outcomes``."""
    slot_of = {slot.impid: index for index, slot in enumerate(request.slots)}
    candidates: list[_Candidate] = []
    for bid in request.bids:
        index = slot_of[bid.impid]
        if (rejected := _ineligible(bid, request.slots[index], request, context)) is not None:
            outcomes[bid.ref] = rejected
        elif _wanted(candidate := _candidate(bid, index, context), lambdas):
            candidates.append(candidate)
        else:
            outcomes[bid.ref] = Outcome.BAR
    return sorted(candidates, key=_Candidate.rank)


@dataclass(slots=True)
class _Pod:
    """The pod being built: winners by slot, the claimed crids/adomains/categories, the durations used.

    ``worst_ms`` is the pod's duration if every winner that has a standalone failed and the standalone played.
    """

    poddur: int
    budget: int
    winners: dict[int, _Candidate] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)
    used_dur: int = 0
    used_ms: int = 0
    worst_ms: int = 0

    def place(self, candidate: _Candidate) -> Outcome:
        if candidate.slot in self.winners:
            return Outcome.OUTBID
        if candidate.claims & self.claimed:
            return Outcome.SEPARATION
        if self.used_dur + candidate.dur > self.poddur or self.used_ms + candidate.vast_dur_ms > self.budget:
            return Outcome.NO_ROOM
        self.winners[candidate.slot] = candidate
        self.claimed |= candidate.claims
        self.used_dur += candidate.dur
        self.used_ms += candidate.vast_dur_ms
        self.worst_ms = self.used_ms
        return Outcome.WON

    def backs(self, winner: _Candidate, candidate: _Candidate) -> bool:
        """Whether ``candidate`` can stand in for ``winner``: same slot and duration, no repeat, and the pod fits
        the break with every standalone so far playing as well."""
        longer = max(candidate.vast_dur_ms - winner.vast_dur_ms, 0)
        return (
            candidate.slot == winner.slot
            and candidate.dur == winner.dur
            and not candidate.claims & self.claimed  # also excludes every winner and standalone so far
            and self.worst_ms + longer <= self.budget
        )

    def back(self, winner: _Candidate, candidate: _Candidate) -> None:
        self.claimed |= candidate.claims
        self.worst_ms += max(candidate.vast_dur_ms - winner.vast_dur_ms, 0)


def _reason(winners: int, slots: int) -> PodReason:
    match winners:
        case 0:
            return PodReason.EMPTY
        case _ if winners == slots:
            return PodReason.FILLED
        case _:
            return PodReason.PARTIAL


def tier0_bids(request: SelectRequest, context: PodContext) -> frozenset[str]:
    """Refs of the bids from tier-0 lines and deals."""
    return frozenset(bid.ref for bid in request.bids if _tier0(bid, context))


def below_floor_bids(request: SelectRequest, context: PodContext) -> frozenset[str]:
    """Refs of the bids under the floor (house ads and tier-0 lines are never under it)."""
    return frozenset(bid.ref for bid in request.bids if _below_floor(bid, context))


def select_pod(request: SelectRequest, context: PodContext = DEFAULT_CONTEXT) -> PodDecision:
    """Select the pod for ``request`` (rules in the module docstring)."""
    slots = request.slots
    lambdas = context.lambdas or slot_lambdas(len(slots))
    if len(lambdas) != len(slots):
        raise ValueError(f"lambdas has {len(lambdas)} entries for {len(slots)} slots")
    pod = _Pod(poddur=min(slot.poddur for slot in slots), budget=limit_ms(request.dur_ms, context.scheduled_ms))
    tier0, below_floor = tier0_bids(request, context), below_floor_bids(request, context)
    if request.dur_ms < MIN_AVAIL_MS:
        outcomes = dict.fromkeys((bid.ref for bid in request.bids), Outcome.SHORT_AVAIL)
        return PodDecision(PodReason.SHORT_AVAIL, pod.budget, pod.poddur, (), (), outcomes, tier0, below_floor)

    outcomes = {}
    candidates = _candidates(request, context, lambdas, outcomes)
    for candidate in candidates:
        outcomes[candidate.bid.ref] = pod.place(candidate)
    winners = [pod.winners[index] for index in sorted(pod.winners)]
    standalones: list[_Candidate] = []
    for winner in winners:
        if (backup := next((c for c in candidates if pod.backs(winner, c)), None)) is not None:
            pod.back(winner, backup)
            standalones.append(backup)
            outcomes[backup.bid.ref] = Outcome.STANDALONE

    return PodDecision(
        reason=_reason(len(winners), len(slots)),
        limit_ms=pod.budget,
        poddur=pod.poddur,
        pod=tuple(map(_placement, winners)),
        standalones=tuple(map(_placement, standalones)),
        outcomes=outcomes,
        tier0=tier0,
        below_floor=below_floor,
    )


TRACKER_EVENT: Final = "impression"


def tracker_url(base: str, decision: str, crid: str, *, enr: int = 0) -> str:
    """Our impression tracker for one ad: ``<base>?d=<sid>.<evid>&c=<crid>&e=impression&enr=<0|1>``.

    ``base`` is the CDN's no-store beacon (``https://<cdn>/ads/b.gif``). MediaTailor fires it server-side and logs it
    as ``BEACON_FIRED`` with these parameters, which is how delivery is counted.
    """
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise ValueError(f"tracker base must be an https URL without query or fragment, got {base!r}")
    if enr not in {0, 1}:
        raise ValueError(f"enr must be 0 or 1, got {enr}")
    query = urlencode({"d": decision, "c": crid, "e": TRACKER_EVENT, "enr": enr}, quote_via=quote, safe="")
    return f"{base}?{query}"


def select_response(
    request: SelectRequest, decision: PodDecision, *, tracker_base: str, enr: int = 0
) -> SelectResponse:
    """The ``/select`` answer for ``decision``: the pod, its standalones and one tracker per ad."""
    ads: Iterable[Placement] = (*decision.pod, *decision.standalones)
    return SelectResponse(
        pod=[PodAd(impid=ad.impid, ref=ad.ref, sequence=n) for n, ad in enumerate(decision.pod, start=1)],
        standalone=[StandaloneAd(impid=ad.impid, ref=ad.ref) for ad in decision.standalones],
        trackers={ad.ref: [tracker_url(tracker_base, request.decision_id, ad.crid, enr=enr)] for ad in ads},
        reason=decision.reason,
    )
