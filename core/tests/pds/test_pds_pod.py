"""Pod rules: the break table measured live with the demo demand as golden vectors, each eligibility rule, and
randomized invariants."""

import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.adload import slot_lambdas
from fast_channels.core.pds.models import AdmKind, Bid, Mode, PodReason, SelectRequest, SelectResponse
from fast_channels.core.pds.pod import (
    C_INS,
    DEFAULT_CONTEXT,
    MIN_AVAIL_MS,
    Outcome,
    PodContext,
    PodDecision,
    limit_ms,
    select_pod,
    select_response,
    tracker_url,
)

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "pds"
SID = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d"
EVID = "820461826"
SLOT_MS = 30030  # a 30 s ad on the 29.97 fps grid
TRACKER = "https://cdn.example.com/ads/b.gif"
TIER0 = PodContext(tier0_deals=frozenset({"fast-direct-1"}))


def bid(ref: str, impid: str = "s1", **fields: Any) -> Bid:
    values: dict[str, Any] = {
        "bidder": "amt",
        "price": Decimal(10),
        "dur": 30,
        "vast_dur_ms": SLOT_MS,
        "crid": ref,
        "adomain": [f"{ref}.example"],
        "adm_kind": AdmKind.INLINE,
    }
    return Bid(ref=ref, impid=impid, **(values | fields))


def house(ref: str, impid: str = "s1", **fields: Any) -> Bid:
    defaults = {"bidder": "ownbook", "house": True, "price": Decimal("0.01"), "adomain": ["publisher.example"]}
    return bid(ref, impid, **(defaults | fields))


def request(
    bids: list[Bid], slots: int = 1, *, dur_ms: int | None = None, mode: Mode | None = Mode.FULL, probe: bool = False
) -> SelectRequest:
    return SelectRequest(
        v=1,
        decision_id=f"{SID}.{EVID}",
        sid=SID,
        pc="fast-demo-p-a",
        evid=EVID,
        dur_ms=slots * SLOT_MS if dur_ms is None else dur_ms,
        mode=mode,
        probe=probe,
        slots=[{"impid": f"s{n}", "poddur": 30 * slots, "rqddurs": [30]} for n in range(1, slots + 1)],
        bids=bids,
    )


def crids(decision: PodDecision) -> tuple[list[str], list[str]]:
    return [ad.crid for ad in decision.pod], [ad.crid for ad in decision.standalones]


# --- golden vectors: the measured break table ------------------------------------------------------------------------

SIM = {
    1: ("sim1", "12.50", "brand-a"),
    2: ("sim2", "22.50", "brand-b"),
    3: ("sim3", "35.00", "brand-c"),
    4: ("sim4", "12.50", "brand-c"),
}


def demo_demand(slots: int, *, simulator: bool = True) -> list[Bid]:
    """The demo demand of the live measurement: the simulator bids sim<n> on imp s<n> (sim3 and sim4 share brand-c);
    the own book bids a house ad per imp (house1, house2 alternating) and the deal line direct1 on the first, here a
    tier-0 demo line."""
    bids = []
    for n in range(1, slots + 1):
        bids.append(house(f"h{n}", f"s{n}", crid="house1" if n % 2 else "house2"))
        if n == 1:
            bids.append(bid("direct1", "s1", bidder="ownbook", price=Decimal(8), dealid="fast-direct-1"))
        if simulator:
            crid, price, brand = SIM[n]
            bids.append(bid(crid, f"s{n}", price=Decimal(price), adomain=[f"{brand}.example"]))
    return bids


@pytest.mark.parametrize(
    ("slots", "pod", "standalones"),
    [
        (4, ["direct1", "sim2", "sim3", "house2"], ["sim1", "house1"]),  # sim4 shares brand-c with sim3
        (3, ["direct1", "sim2", "sim3"], ["sim1", "house2", "house1"]),
        (2, ["direct1", "sim2"], ["sim1", "house2"]),
        (1, ["direct1"], ["sim1"]),
    ],
)
def test_demo_full_pods(slots: int, pod: list[str], standalones: list[str]) -> None:
    decision = select_pod(request(demo_demand(slots), slots), TIER0)
    assert crids(decision) == (pod, standalones)
    assert decision.reason is PodReason.FILLED
    assert decision.filled_ms == slots * SLOT_MS
    assert decision.tier0 == {"direct1"}


def test_demo_tier0_line_beats_a_higher_bid() -> None:
    # sim1 bids $12.50 against direct1's $8 in slot 1; as an open deal (not tier-0) it would lose the slot
    assert crids(select_pod(request(demo_demand(1), 1), TIER0))[0] == ["direct1"]
    assert crids(select_pod(request(demo_demand(1), 1)))[0] == ["sim1"]


def test_demo_own_book_only_gives_a_partial_pod_the_slate_completes() -> None:
    decision = select_pod(request(demo_demand(4, simulator=False), 4, mode=Mode.CONTEXTUAL), TIER0)
    assert crids(decision) == (["direct1", "house2", "house1"], [])  # the fourth slot would repeat house2
    assert decision.reason is PodReason.PARTIAL
    assert decision.filled_ms == 3 * SLOT_MS
    assert decision.outcomes["h4"] is Outcome.SEPARATION


def test_demo_adomain_separation_spans_bidders() -> None:
    decision = select_pod(request(demo_demand(4), 4), TIER0)
    assert decision.outcomes["sim4"] is Outcome.SEPARATION
    assert decision.outcomes["sim1"] is Outcome.STANDALONE


def _fixtures(call: str) -> list[Path]:
    return sorted((FIXTURES / call).glob("*.json"))


@pytest.mark.parametrize("path", _fixtures("select"), ids=lambda path: path.stem)
def test_select_fixtures_hold_what_the_rules_answer(path: Path) -> None:
    document = json.loads(path.read_text())
    select = SelectRequest.model_validate(document["request"])
    context = document["context"]
    decision = select_pod(select, PodContext.model_validate(context["pod"]))
    answer = select_response(select, decision, tracker_base=context["tracker_base"], enr=context["enr"])
    assert SelectResponse.model_validate(document["response"]) == answer
    assert answer.model_dump(mode="json") == document["response"]


def test_select_fixtures_cover_the_measured_table() -> None:
    names = {path.stem for path in _fixtures("select")}
    assert {f"demo-full-{s}s" for s in (30, 60, 90, 120)} | {"demo-own-book-120s", "short-avail"} <= names


# --- eligibility ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"adm_kind": None}, Outcome.INVALID_VAST),
        ({"vast_dur_ms": None}, Outcome.NO_DURATION),
        ({"dur": None}, Outcome.NO_DURATION),
        ({"dur": 0}, Outcome.NO_DURATION),
        ({"vast_dur_ms": 30_500}, Outcome.DURATION_MISMATCH),  # rounds half up to 31 s
        ({"dur": 15, "vast_dur_ms": 15_015}, Outcome.RQDDURS),
        ({"crid": None}, Outcome.NO_CRID),
        ({"crid": ""}, Outcome.NO_CRID),
        ({"adomain": []}, Outcome.NO_ADOMAIN),
        ({"price": Decimal("0.99")}, Outcome.FLOOR),
    ],
)
def test_ineligible_bids(change: dict[str, Any], outcome: Outcome) -> None:
    decision = select_pod(request([bid("x", **change)]), PodContext(floor_cpm=Decimal(1)))
    assert decision.outcomes == {"x": outcome}
    assert decision.reason is PodReason.EMPTY
    assert not decision.pod


def test_a_vast_duration_on_the_grid_matches_its_whole_seconds() -> None:
    for vast_dur_ms in (29_500, 30_000, 30_030, 30_499):
        assert select_pod(request([bid("x", vast_dur_ms=vast_dur_ms)], dur_ms=30_499)).outcomes == {"x": Outcome.WON}


def test_house_ads_are_exempt_from_floors_and_adomain() -> None:
    decision = select_pod(request([house("h", adomain=[], price=Decimal(0))]), PodContext(floor_cpm=Decimal(5)))
    assert decision.outcomes == {"h": Outcome.WON}


@pytest.mark.parametrize("mode", [Mode.SAFE, Mode.KIDS, None])
def test_own_book_only_without_a_third_party_mode(mode: Mode | None) -> None:
    bids = [bid("third"), bid("line", bidder="ownbook", price=Decimal(1))]
    decision = select_pod(request(bids, mode=mode))
    assert decision.outcomes == {"third": Outcome.MODE, "line": Outcome.WON}


def test_probe_sessions_get_house_ads_only() -> None:
    bids = [bid("line", bidder="ownbook", price=Decimal(9)), house("h")]
    decision = select_pod(request(bids, mode=Mode.SAFE, probe=True))
    assert decision.outcomes == {"line": Outcome.PROBE, "h": Outcome.WON}


def test_recent_creatives_are_not_repeated() -> None:
    decision = select_pod(request([bid("seen"), bid("fresh", price=Decimal(1))]), PodContext(recent_crids={"seen"}))
    assert decision.outcomes == {"seen": Outcome.RECENT, "fresh": Outcome.WON}


# --- value and the ad-load price --------------------------------------------------------------------------------------


def test_paid_bids_rank_by_accepted_net_value() -> None:
    bids = [bid("a", bidder="a", price=Decimal(20)), bid("b", bidder="b", price=Decimal(15))]
    assert crids(select_pod(request(bids)))[0] == ["a"]
    halved = PodContext(accept_ratio={"a": Decimal("0.5")})  # a's ads play half the time: $10 accepted
    assert crids(select_pod(request(bids), halved)) == (["b"], ["a"])


def test_a_bid_worth_less_than_an_insertion_does_not_run() -> None:
    worth = C_INS * 1000  # CPM at which net value is exactly 0
    decision = select_pod(request([bid("x", price=worth), house("h")]))
    assert decision.outcomes == {"x": Outcome.BAR, "h": Outcome.WON}


def test_lambda_prices_ad_load_per_slot() -> None:
    bids = [bid("x", "s1", price=Decimal(5)), bid("y", "s2", price=Decimal(50)), house("h", "s1")]
    lam = PodContext(lambdas=(Decimal("0.01"), Decimal("0.01")))  # $10 CPM-equivalent per slot
    decision = select_pod(request(bids, 2), lam)
    assert decision.outcomes == {"x": Outcome.BAR, "h": Outcome.BAR, "y": Outcome.WON}
    assert decision.reason is PodReason.PARTIAL


def test_tier0_runs_whatever_lambda_says() -> None:
    line = bid("line", bidder="ownbook", price=Decimal("0.10"), dealid="pg")
    decision = select_pod(request([line]), PodContext(tier0_deals={"pg"}, lambdas=(Decimal(1),)))
    assert decision.outcomes == {"line": Outcome.WON}
    assert decision.tier0 == {"line"}


def test_the_lambda_stub_prices_nothing() -> None:
    assert slot_lambdas(3) == (Decimal(0),) * 3
    assert slot_lambdas(0) == ()
    context = PodContext(lambdas=slot_lambdas(2))
    assert crids(select_pod(request(demo_demand(2), 2), context)) == crids(select_pod(request(demo_demand(2), 2)))
    with pytest.raises(ValueError, match="negative"):
        slot_lambdas(-1)


def test_lambdas_must_match_the_slots() -> None:
    with pytest.raises(ValueError, match="2 entries for 1 slots"):
        select_pod(request([bid("x")]), PodContext(lambdas=slot_lambdas(2)))


# --- separation, durations, standalones -------------------------------------------------------------------------------


def test_one_iab_category_per_break() -> None:
    bids = [
        bid("a", "s1", cat=["IAB2"], price=Decimal(20)),
        bid("b", "s2", cat=["IAB2"], price=Decimal(10)),
        bid("c", "s2", cat=["IAB2"], cattax=2, price=Decimal(5)),  # same code in another taxonomy
    ]
    assert select_pod(request(bids, 2)).outcomes == {"a": Outcome.WON, "b": Outcome.SEPARATION, "c": Outcome.WON}


def test_creatives_are_deduplicated_across_bidders() -> None:
    bids = [bid("a", "s1", crid="same"), bid("b", "s2", bidder="other", crid="same", adomain=["b.example"])]
    assert select_pod(request(bids, 2)).outcomes == {"a": Outcome.WON, "b": Outcome.SEPARATION}


def test_adomain_comparison_ignores_case_and_spaces() -> None:
    bids = [bid("a", "s1", adomain=["Brand.Example"]), bid("b", "s2", adomain=[" brand.example"])]
    assert select_pod(request(bids, 2)).outcomes["b"] is Outcome.SEPARATION


def test_house_ads_share_the_publisher_domain_but_never_a_creative() -> None:
    bids = [house("h1", "s1", crid="promo1"), house("h2", "s2", crid="promo2"), house("h3", "s3", crid="promo1")]
    decision = select_pod(request(bids, 3, mode=Mode.SAFE))
    assert crids(decision) == (["promo1", "promo2"], [])
    assert decision.outcomes["h3"] is Outcome.SEPARATION


def test_pod_durations_never_exceed_the_break() -> None:
    bids = [bid("a", "s1"), bid("b", "s2")]
    decision = select_pod(request(bids, 2, dur_ms=60_000))  # 2 x 30.03 s does not fit 60.000 s
    assert decision.outcomes == {"a": Outcome.WON, "b": Outcome.NO_ROOM}
    assert decision.limit_ms == 60_000


def test_prefetched_breaks_are_checked_against_the_schedule_of_record() -> None:
    bids = [bid("a", "s1"), bid("b", "s2")]
    decision = select_pod(request(bids, 2, dur_ms=60_000), PodContext(scheduled_ms=60_060))
    assert crids(decision)[0] == ["a", "b"]
    assert decision.limit_ms == 60_060


def test_limit_ms() -> None:
    assert limit_ms(60_060, None) == 60_060
    assert limit_ms(60_000, 60_060) == 60_060  # the prefetch retrieval's rounding
    assert limit_ms(120_000, 120_120) == 120_120
    assert limit_ms(60_060, 60_060) == 60_060
    assert limit_ms(108_120, 120_120) == 108_120  # a remainder pod after a late join stays the remainder
    assert limit_ms(59_000, 60_060) == 59_000  # not the schedule's rounding
    assert limit_ms(60_000, 60_000) == 60_000


def test_sum_of_declared_durations_fits_poddur() -> None:
    two = request([bid("a", "s1"), bid("b", "s2")], 2, dur_ms=90_090)
    two = two.model_copy(update={"slots": [s.model_copy(update={"poddur": 30}) for s in two.slots]})
    assert select_pod(two).outcomes == {"a": Outcome.WON, "b": Outcome.NO_ROOM}
    assert select_pod(two).poddur == 30


def test_a_standalone_backs_its_own_slot_with_the_same_duration() -> None:
    slots = request([], 2).slots
    mixed = [s.model_copy(update={"rqddurs": [15, 30]}) for s in slots]
    bids = [
        bid("a", "s1", price=Decimal(9)),
        bid("b", "s2", price=Decimal(8)),
        bid("short", "s1", dur=15, vast_dur_ms=15_015, price=Decimal(1)),  # another duration
        bid("other-slot", "s2", price=Decimal(7)),
        bid("backup", "s1", price=Decimal(6)),
    ]
    select = request(bids, 2).model_copy(update={"slots": mixed})
    decision = select_pod(select)
    assert crids(decision) == (["a", "b"], ["backup", "other-slot"])
    assert decision.outcomes["short"] is Outcome.OUTBID


def test_a_standalone_must_keep_the_pod_within_the_break() -> None:
    bids = [bid("a", "s1", vast_dur_ms=29_600), bid("b", "s2"), bid("long", "s1", price=Decimal(1), vast_dur_ms=30_499)]
    decision = select_pod(request(bids, 2, dur_ms=59_700))  # 29.6 + 30.03 fits; 30.499 + 30.03 does not
    assert crids(decision) == (["a", "b"], [])
    assert decision.outcomes["long"] is Outcome.OUTBID


def test_avails_under_30_s_get_an_empty_pod() -> None:
    decision = select_pod(request([bid("x"), house("h")], dur_ms=MIN_AVAIL_MS - 30))
    assert decision.reason is PodReason.SHORT_AVAIL
    assert decision.outcomes == {"x": Outcome.SHORT_AVAIL, "h": Outcome.SHORT_AVAIL}
    assert not decision.pod and not decision.standalones
    answer = select_response(request([bid("x")]), decision, tracker_base=TRACKER)
    assert answer.model_dump(mode="json") == {
        "v": 1,
        "pod": [],
        "standalone": [],
        "trackers": {},
        "reason": "short_avail",
    }


def test_a_request_without_bids_is_empty() -> None:
    assert select_pod(request([])).reason is PodReason.EMPTY
    assert DEFAULT_CONTEXT.own_book == {"ownbook"}


# --- the answer and its trackers --------------------------------------------------------------------------------------


def test_select_response_sequences_the_pod_and_tracks_every_ad() -> None:
    select = request(demo_demand(2), 2)
    answer = select_response(select, select_pod(select, TIER0), tracker_base=TRACKER, enr=1)
    assert [(ad.impid, ad.ref, ad.sequence) for ad in answer.pod] == [("s1", "direct1", 1), ("s2", "sim2", 2)]
    assert [(ad.impid, ad.ref) for ad in answer.standalone] == [("s1", "sim1"), ("s2", "h2")]
    assert set(answer.trackers) == {"direct1", "sim2", "sim1", "h2"}
    assert answer.trackers["h2"] == [f"{TRACKER}?d={SID}.{EVID}&c=house2&e=impression&enr=1"]


def test_tracker_url_encodes_third_party_creative_ids() -> None:
    url = tracker_url(TRACKER, "s.1", "a b&c=/d")
    assert url == f"{TRACKER}?d=s.1&c=a%20b%26c%3D%2Fd&e=impression&enr=0"


@pytest.mark.parametrize(
    "base", ["http://cdn.example.com/ads/b.gif", "https:///b.gif", f"{TRACKER}?x=1", f"{TRACKER}#f"]
)
def test_tracker_base_is_a_plain_https_url(base: str) -> None:
    with pytest.raises(ValueError, match="https URL"):
        tracker_url(base, "d", "c")


def test_tracker_enr_is_a_bit() -> None:
    with pytest.raises(ValueError, match="enr"):
        tracker_url(TRACKER, "d", "c", enr=2)


# --- randomized invariants --------------------------------------------------------------------------------------------


def _random_request(rng: random.Random) -> tuple[SelectRequest, PodContext]:
    slots = rng.randint(1, 4)
    durations = (15, 30)
    bids = []
    for n in range(rng.randint(0, 24)):
        dur = rng.choice(durations)
        is_house = rng.random() < 0.25
        bids.append(
            Bid(
                ref=f"b{n}",
                bidder="ownbook" if is_house or rng.random() < 0.3 else rng.choice(["amt", "x", "y"]),
                impid=f"s{rng.randint(1, slots)}",
                price=Decimal(rng.randint(0, 4000)) / 100,
                dur=dur,
                vast_dur_ms=dur * 1000 + rng.choice([-400, 0, 15, 30, 499]),
                crid=f"c{rng.randint(0, 12)}",
                adomain=[f"d{rng.randint(0, 8)}.example"],
                cat=rng.sample(["IAB1", "IAB2", "IAB3", "IAB17"], rng.randint(0, 2)),
                dealid=rng.choice([None, None, "pg1", "deal2"]),
                house=is_house,
                adm_kind=rng.choice([AdmKind.INLINE, AdmKind.INLINE, AdmKind.WRAPPER, None]),
            )
        )
    select = SelectRequest(
        v=1,
        decision_id=f"{SID}.{EVID}",
        sid=SID,
        pc="fast-demo-p-a",
        evid=EVID,
        dur_ms=rng.choice([slots * SLOT_MS, slots * 30_000, slots * SLOT_MS - 1000, 29_970]),
        mode=rng.choice([*Mode, None]),
        probe=rng.random() < 0.1,
        slots=[{"impid": f"s{k}", "poddur": 30 * slots, "rqddurs": list(durations)} for k in range(1, slots + 1)],
        bids=bids,
    )
    context = PodContext(
        tier0_deals=frozenset({"pg1"}),
        floor_cpm=Decimal(rng.choice([0, 1, 5])),
        recent_crids=frozenset({f"c{rng.randint(0, 12)}"}),
        scheduled_ms=rng.choice([None, slots * SLOT_MS]),
        lambdas=tuple(Decimal(rng.choice([0, 0, 0, 5])) / 1000 for _ in range(slots)),
    )
    return select, context


def test_randomized_pods_keep_every_invariant() -> None:
    rng = random.Random(20261028)
    for _ in range(3000):
        select, context = _random_request(rng)
        decision = select_pod(select, context)
        by_ref = {b.ref: b for b in select.bids}
        placed = [by_ref[ad.ref] for ad in (*decision.pod, *decision.standalones)]
        sold = [b for b in placed if not b.house]
        assert len({b.crid for b in placed}) == len(placed), "a creative repeats"
        assert len({d for b in sold for d in b.adomain}) == sum(len(b.adomain) for b in sold), "an advertiser repeats"
        assert len({(b.cattax or 1, c) for b in sold for c in b.cat}) == sum(len(b.cat) for b in sold)
        assert sum(ad.dur for ad in decision.pod) <= decision.poddur
        assert decision.filled_ms <= decision.limit_ms, "the pod is longer than the break"
        assert len({ad.slot for ad in decision.pod}) == len(decision.pod), "two ads in one slot"
        assert [ad.slot for ad in decision.pod] == sorted(ad.slot for ad in decision.pod)
        assert set(decision.outcomes) == set(by_ref)
        for backup in decision.standalones:
            (winner,) = [ad for ad in decision.pod if ad.slot == backup.slot]
            assert backup.dur == winner.dur
            assert decision.filled_ms - winner.vast_dur_ms + backup.vast_dur_ms <= decision.limit_ms
        if select.dur_ms < MIN_AVAIL_MS:
            assert not decision.pod
        if select.mode in {Mode.SAFE, Mode.KIDS, None}:
            assert all(b.bidder == "ownbook" for b in placed)
        if select.probe:
            assert all(b.house for b in placed)
        shuffled = select.model_copy(update={"bids": rng.sample(select.bids, len(select.bids))})
        assert select_pod(shuffled, context) == decision, "the result depends on bid order"
