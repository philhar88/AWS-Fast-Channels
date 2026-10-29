"""``/select``'s decision: the pod rules, with every own-book line ad in the answer reserved (M2-04, ADR-0022).

The pod rules (``pod.select_pod``) choose the pod and its standalones. Each own-book line ad among them (not a house
ad) needs a reservation of its price before it may be answered, because the answer can play. So:

1. select the pod;
2. reserve the line ads it holds that are not reserved yet (``reserve``: line -> crids, returns the crids refused:
   no budget left, the session's cap reached, or the reservation failed). A refusal is the line's, so
3. if any was refused, select again without that line's bids (those not reserved yet), and reserve again.

After :data:`ATTEMPTS` rounds that still met refusals, the pod is selected once more without any line ad that is not
reserved, so the answer never holds an unreserved line ad. An own-book bid that is neither a house ad nor a creative
of a line in the snapshot (the own book saw a newer snapshot, say) is left out from the start: it cannot be reserved.
A reservation made for an ad that a later round dropped stays until the sweeper releases it: liability is
over-reserved for two hours at most, never under.

Standalones are reserved like winners: MediaTailor plays a standalone when the winner fails, so either can be the
impression.
"""

from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from fast_channels.core.pds.book import Book, Line
from fast_channels.core.pds.pod import Outcome, PodContext, PodDecision, below_floor_bids, select_pod, tier0_bids

if TYPE_CHECKING:
    from fast_channels.core.pds.models import SelectRequest

ATTEMPTS: Final = 3
"""Rounds of selection and reservation before the line ads not reserved are left out."""
PLACEMENT: Final = frozenset({Outcome.WON, Outcome.STANDALONE, Outcome.OUTBID, Outcome.SEPARATION, Outcome.NO_ROOM})
"""Outcomes of an eligible bid: one left out for its reservation is recorded ``unreserved`` instead."""

type Reserve = Callable[[Mapping[Line, tuple[str, ...]]], frozenset[str]]
"""Reserve each line's crids; returns the crids refused."""


def pod_context(book: Book, *, floor_cpm: Decimal = Decimal(0), scheduled: int | None = None) -> PodContext:
    """The pod rules' context: the own book's tier-0 deals, the floor and the break of record. No session history
    yet (``recent_crids``), no creative approved for kids breaks, every bidder's ads accepted."""
    return PodContext(tier0_deals=book.tier0_deals, floor_cpm=floor_cpm, scheduled_ms=scheduled)


def _without(request: SelectRequest, refs: frozenset[str]) -> SelectRequest:
    if not refs:
        return request
    return request.model_copy(update={"bids": [bid for bid in request.bids if bid.ref not in refs]})


def select(
    request: SelectRequest, rules: PodContext, book: Book, *, reserve: Reserve
) -> tuple[PodDecision, frozenset[str]]:
    """The pod for ``request`` and the crids of its line ads, every one reserved (by this call or an earlier one)."""
    sold = {bid.ref: bid for bid in request.bids if bid.bidder in rules.own_book and not bid.house}
    lines = {ref: (bid.crid, line) for ref, bid in sold.items() if bid.crid and (line := book.line_of(bid.crid))}
    unknown = frozenset(sold) - frozenset(lines)
    held: set[str] = set()
    refused: set[str] = set()  # line ids
    excluded = unknown
    baseline = decision = select_pod(_without(request, excluded), rules)
    for attempt in range(ATTEMPTS):
        if attempt:
            decision = select_pod(_without(request, excluded), rules)
        wanted: dict[Line, list[str]] = {}
        for ad in (*decision.pod, *decision.standalones):
            if ad.ref in lines and (crid := lines[ad.ref][0]) not in held:
                crids = wanted.setdefault(lines[ad.ref][1], [])
                if crid not in crids:
                    crids.append(crid)
        if not wanted:
            break
        denied = reserve({line: tuple(crids) for line, crids in wanted.items()})
        held |= {crid for crids in wanted.values() for crid in crids} - denied
        refused |= {line.id for line, crids in wanted.items() if denied & set(crids)}
        if not denied:
            break
        excluded = unknown | {ref for ref, (crid, line) in lines.items() if line.id in refused and crid not in held}
    else:
        excluded = unknown | {ref for ref, (crid, _) in lines.items() if crid not in held}
        decision = select_pod(_without(request, excluded), rules)
    answered = {ad.crid for ad in (*decision.pod, *decision.standalones)}
    return _finish(request, rules, decision, baseline, excluded), frozenset(held & answered)


def _finish(
    request: SelectRequest, rules: PodContext, decision: PodDecision, baseline: PodDecision, excluded: frozenset[str]
) -> PodDecision:
    """``decision`` with an outcome for every bid of ``request``: a bid left out keeps the first round's outcome if
    it was not eligible anyway, else it is ``unreserved``; the tier-0 and below-floor bids are the request's."""
    if not excluded:
        return decision

    def left_out(ref: str) -> Outcome:
        first = baseline.outcomes.get(ref)
        return first if first is not None and first not in PLACEMENT else Outcome.UNRESERVED

    outcomes = {**{ref: left_out(ref) for ref in excluded}, **decision.outcomes}
    return replace(
        decision,
        outcomes={bid.ref: outcomes[bid.ref] for bid in request.bids},
        tier0=tier0_bids(request, rules),
        below_floor=below_floor_bids(request, rules),
    )
