"""``/select``'s decision: the pod rules, with every own-book line ad in the answer reserved (M2-04, ADR-0022).

The pod rules (``pod.select_pod``) choose the pod and its standalones. Each own-book line ad among them (not a house
ad) needs a reservation of its price before it may be answered, because the answer can play. So:

1. select the pod;
2. reserve the line ads it holds that are not reserved yet (``reserve``: line -> crids, returns the crids refused:
   no budget left, the session's cap reached, or the reservation failed);
3. if any was refused, select again without that creative's bids, and reserve again.

After :data:`ATTEMPTS` rounds that still met refusals, the pod is selected once more without any line ad that is not
reserved, so the answer never holds an unreserved line ad. A creative left out this way is recorded as
``unreserved``. A reservation made for an ad that a later round dropped stays until the sweeper releases it: liability
is over-reserved for two hours at most, never under.

Standalones are reserved like winners: MediaTailor plays a standalone when the winner fails, so either can be the
impression.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from fast_channels.core.pds.book import Book, Line
from fast_channels.core.pds.pod import Outcome, PodContext, PodDecision, select_pod
from fast_channels.core.timeline import whole_seconds

if TYPE_CHECKING:
    from fast_channels.core.pds.models import SelectRequest

ATTEMPTS: Final = 3
"""Rounds of selection and reservation before the line ads not reserved are left out."""

type Reserve = Callable[[Mapping[Line, tuple[str, ...]]], frozenset[str]]
"""Reserve each line's crids; returns the crids refused."""


def scheduled_ms(dur_ms: int, breaks_ms: Iterable[int]) -> int | None:
    """The schedule of record's break that ``dur_ms`` is the prefetch rounding of (60,000 for 60,060), or None."""
    return next((ms for ms in breaks_ms if ms != dur_ms and whole_seconds(ms) * 1000 == dur_ms), None)


def pod_context(book: Book, *, floor_cpm: Decimal = Decimal(0), scheduled: int | None = None) -> PodContext:
    """The pod rules' context: the own book's tier-0 deals, the floor and the break of record. No session history
    yet (``recent_crids``), no creative approved for kids breaks, every bidder's ads accepted."""
    return PodContext(tier0_deals=book.tier0_deals, floor_cpm=floor_cpm, scheduled_ms=scheduled)


def _without(request: SelectRequest, refs: frozenset[str]) -> SelectRequest:
    if not refs:
        return request
    return request.model_copy(update={"bids": [bid for bid in request.bids if bid.ref not in refs]})


def _line_bids(request: SelectRequest, book: Book, own_book: frozenset[str]) -> dict[str, tuple[str, Line]]:
    """Ref -> (crid, line) of the own book's line bids (a house ad is never a line's)."""
    found: dict[str, tuple[str, Line]] = {}
    for bid in request.bids:
        if bid.bidder in own_book and not bid.house and bid.crid and (line := book.line_of(bid.crid)) is not None:
            found[bid.ref] = (bid.crid, line)
    return found


def select(
    request: SelectRequest, pod_context: PodContext, book: Book, *, reserve: Reserve
) -> tuple[PodDecision, frozenset[str]]:
    """The pod for ``request`` and the crids reserved for it (this call's and earlier calls' for the break)."""
    lines = _line_bids(request, book, pod_context.own_book)
    held: set[str] = set()
    refused: set[str] = set()
    for _ in range(ATTEMPTS):
        excluded = frozenset(ref for ref, (crid, _) in lines.items() if crid in refused)
        decision = select_pod(_without(request, excluded), pod_context)
        wanted: dict[Line, list[str]] = {}
        for ad in (*decision.pod, *decision.standalones):
            if ad.ref not in lines:
                continue
            crid, line = lines[ad.ref]
            if crid not in held and crid not in wanted.get(line, []):
                wanted.setdefault(line, []).append(crid)
        if not wanted:
            return _finish(request, decision, excluded), frozenset(held)
        denied = reserve({line: tuple(crids) for line, crids in wanted.items()})
        held |= {crid for crids in wanted.values() for crid in crids} - denied
        refused |= denied
        if not denied:
            return _finish(request, decision, excluded), frozenset(held)
    excluded = frozenset(ref for ref, (crid, _) in lines.items() if crid not in held)
    decision = select_pod(_without(request, excluded), pod_context)
    return _finish(request, decision, excluded), frozenset(held)


def _finish(request: SelectRequest, decision: PodDecision, excluded: frozenset[str]) -> PodDecision:
    """``decision`` with an outcome for every bid of ``request``: the bids left out are ``unreserved``."""
    if not excluded:
        return decision
    outcomes = {**dict.fromkeys(excluded, Outcome.UNRESERVED), **decision.outcomes}
    return replace(decision, outcomes={bid.ref: outcomes[bid.ref] for bid in request.bids})
