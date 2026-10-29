"""/select's decision with reservations (M2-04): the pod rules, every line ad in the answer reserved."""

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.pds.book import Book, Line
from fast_channels.core.pds.models import SelectRequest
from fast_channels.core.pds.pod import Outcome, scheduled_ms, select_pod
from fast_channels.core.pds.selection import ATTEMPTS, pod_context, select

FIXTURES = Path(__file__).parents[3] / "contracts" / "fixtures" / "pds" / "select"
DIRECT1 = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal(8),
    budget=Decimal(100),
    tier0=True,
)
BOOK = Book(lines=(DIRECT1,))
RULES = pod_context(BOOK)


def fixture(name: str) -> SelectRequest:
    return SelectRequest.model_validate(json.loads((FIXTURES / name).read_text())["request"])


class Reservations:
    """A fake reservation step: refuses the crids it is told to, records what it was asked."""

    def __init__(self, refuse: frozenset[str] = frozenset()) -> None:
        self.refuse = refuse
        self.asked: list[dict[str, tuple[str, ...]]] = []

    def __call__(self, wanted: Mapping[Line, tuple[str, ...]]) -> frozenset[str]:
        self.asked.append({line.id: crids for line, crids in wanted.items()})
        return frozenset(crid for crids in wanted.values() for crid in crids if crid in self.refuse)


def refs(decision: Any) -> list[str]:
    return [ad.ref for ad in decision.pod]


def test_the_pod_is_the_rules_and_its_line_ad_is_reserved() -> None:
    request = fixture("demo-full-120s.json")
    reservations = Reservations()

    decision, reserved = select(request, RULES, BOOK, reserve=reservations)

    assert decision == select_pod(request, RULES)
    assert refs(decision) == ["ownbook-s1-direct1", "amt-s2-sim2", "amt-s3-sim3", "ownbook-s4-house2"]
    assert (reserved, reservations.asked) == (frozenset({"direct1"}), [{"direct-1": ("direct1",)}])


def test_a_line_that_cannot_be_reserved_is_left_out_and_recorded() -> None:
    request = fixture("demo-full-120s.json")
    reservations = Reservations(refuse=frozenset({"direct1"}))

    decision, reserved = select(request, RULES, BOOK, reserve=reservations)

    assert reserved == frozenset()
    assert refs(decision) == ["amt-s1-sim1", "amt-s2-sim2", "amt-s3-sim3", "ownbook-s4-house2"]
    assert decision.outcomes["ownbook-s1-direct1"] is Outcome.UNRESERVED
    assert set(decision.outcomes) == {bid.ref for bid in request.bids}
    assert reservations.asked == [{"direct-1": ("direct1",)}], "the second round holds no line ad"


def test_standalones_are_reserved_too() -> None:
    request = fixture("demo-full-120s.json")
    line = DIRECT1.model_copy(update={"tier0": False, "cpm": Decimal(1)})
    book = Book(lines=(line,))
    reservations = Reservations()

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert [ad.ref for ad in decision.standalones][:1] == ["ownbook-s1-direct1"], "a cheap line backs sim1"
    assert reserved == frozenset({"direct1"})


def test_house_ads_and_third_parties_are_never_reserved() -> None:
    request = fixture("demo-own-book-120s.json")
    book = Book()
    reservations = Reservations()

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert decision.pod and (reserved, reservations.asked) == (frozenset(), [])


def test_after_the_attempts_every_unreserved_line_ad_is_left_out() -> None:
    lines = tuple(
        DIRECT1.model_copy(update={"id": f"line-{n}", "creatives": (f"c{n}",), "tier0": False}) for n in range(5)
    )
    book = Book(lines=lines)
    bids = [
        {
            "ref": f"ownbook-s1-c{n}",
            "bidder": "ownbook",
            "seat": "ownbook",
            "impid": "s1",
            "price": 20 - n,
            "dur": 30,
            "vast_dur_ms": 30030,
            "crid": f"c{n}",
            "adomain": [f"d{n}.example"],
            "cat": [],
            "cattax": None,
            "dealid": None,
            "house": False,
            "has_impression": False,
            "adm_kind": "InLine",
        }
        for n in range(5)
    ]
    data = json.loads((FIXTURES / "demo-own-book-120s.json").read_text())["request"]
    request = SelectRequest.model_validate({**data, "dur_ms": 30030, "slots": data["slots"][:1], "bids": bids})
    reservations = Reservations(refuse=frozenset({f"c{n}" for n in range(5)}))

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert len(reservations.asked) == ATTEMPTS
    assert (decision.pod, reserved) == ((), frozenset())
    assert {decision.outcomes[bid.ref] for bid in request.bids} == {Outcome.UNRESERVED}


def test_a_crid_held_in_an_earlier_round_is_not_asked_again() -> None:
    request = fixture("demo-full-120s.json")
    other = Line(
        id="direct-2",
        advertiser="Direct Advertiser 2",
        adomain="direct2.example",
        creatives=("sim2",),
        cpm=Decimal(1),
        budget=Decimal(1),
    )
    book = Book(lines=(DIRECT1, other))
    own = [bid.model_copy(update={"bidder": "ownbook"}) if bid.crid == "sim2" else bid for bid in request.bids]
    request = request.model_copy(update={"bids": own})
    reservations = Reservations(refuse=frozenset({"sim2"}))

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert reservations.asked == [{"direct-1": ("direct1",), "direct-2": ("sim2",)}]
    assert reserved == frozenset({"direct1"})
    assert "ownbook-s1-direct1" in refs(decision)


@pytest.mark.parametrize(
    ("dur_ms", "expected"),
    [(60000, 60060), (60060, None), (120000, 120120), (45000, None), (30000, 30030)],
)
def test_a_prefetched_break_is_the_schedules(dur_ms: int, expected: int | None) -> None:
    assert scheduled_ms(dur_ms, (30030, 60060, 90090, 120120)) == expected


def test_the_context_is_the_books() -> None:
    rules = pod_context(BOOK, floor_cpm=Decimal(2), scheduled=60060)

    assert (rules.tier0_deals, rules.floor_cpm, rules.scheduled_ms) == (frozenset({"fast-direct-1"}), 2, 60060)
    assert rules.recent_crids == frozenset() and rules.kids_approved == frozenset()


def test_an_own_book_ad_of_no_known_line_is_left_out() -> None:
    request = fixture("demo-full-120s.json")
    reservations = Reservations()

    decision, reserved = select(request, RULES, Book(), reserve=reservations)

    assert "ownbook-s1-direct1" not in refs(decision)
    assert decision.outcomes["ownbook-s1-direct1"] is Outcome.UNRESERVED
    assert reservations.asked == [] and reserved == frozenset()
    assert "ownbook-s1-direct1" in decision.tier0, "the first round's tier-0 bids"


def test_a_refused_line_is_left_out_whole() -> None:
    line = DIRECT1.model_copy(update={"creatives": ("direct1", "direct1b")})
    book = Book(lines=(line,))
    request = fixture("demo-full-120s.json")
    twin = next(b for b in request.bids if b.crid == "direct1").model_copy(
        update={"ref": "ownbook-s1-direct1b", "crid": "direct1b", "adomain": ["direct1b.example"], "price": Decimal(7)}
    )
    request = request.model_copy(update={"bids": [*request.bids, twin]})
    reservations = Reservations(refuse=frozenset({"direct1", "direct1b"}))

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert len(reservations.asked) == 1, "one refusal takes the line out: no round for its other creative"
    assert {decision.outcomes[r] for r in ("ownbook-s1-direct1", "ownbook-s1-direct1b")} == {Outcome.UNRESERVED}
    assert reserved == frozenset()


def test_only_answered_line_ads_are_reported_reserved() -> None:
    other = Line(
        id="direct-2",
        advertiser="Direct Advertiser 2",
        adomain="direct2.example",
        creatives=("sim2",),
        cpm=Decimal(1),
        budget=Decimal(1),
    )
    book = Book(lines=(DIRECT1, other))
    request = fixture("demo-full-120s.json")
    own = [bid.model_copy(update={"bidder": "ownbook"}) if bid.crid == "sim2" else bid for bid in request.bids]
    request = request.model_copy(update={"bids": own})
    reservations = Reservations(refuse=frozenset({"direct1"}))

    decision, reserved = select(request, pod_context(book), book, reserve=reservations)

    assert "sim2" in {ad.crid for ad in decision.pod}
    assert reserved == frozenset({"sim2"})


def test_a_left_out_bid_that_was_not_eligible_keeps_its_outcome() -> None:
    request = fixture("demo-full-120s.json")
    cheap = DIRECT1.model_copy(update={"tier0": False})
    book = Book(lines=(cheap,))
    floor = pod_context(book, floor_cpm=Decimal(9))
    reservations = Reservations()

    data = request.model_copy(update={"bids": [b for b in request.bids if b.bidder == "ownbook"]})
    decision, _ = select(data, floor, book, reserve=Reservations(refuse=frozenset({"x"})))
    assert decision.outcomes["ownbook-s1-direct1"] is Outcome.FLOOR
    assert "ownbook-s1-direct1" in decision.below_floor
    assert reservations.asked == []
