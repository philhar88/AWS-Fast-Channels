from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fast_channels.core.recs.candidates import Candidate, Cohort, Taste
from fast_channels.core.recs.cards import Card
from fast_channels.core.recs.rail import Daypart
from fast_channels.core.recs.validate import (
    COHORT_TITLE,
    PERSONAL_TITLE,
    audit_prompt,
    holds,
    template,
    top_channel_limit,
    validate,
)

NOW = datetime(2026, 11, 10, 19, tzinfo=UTC)
DANA = Taste(genres=("sci-fi", "action", "fantasy"))
EVENING = Cohort(device="web", language="en", daypart=Daypart.EVENING)


def make_card(content_id: str, title: str, genres: tuple[str, ...], rating: str = "TV-14", **extra: Any) -> Card:
    return Card(content_id=content_id, title=title, genres=genres, rating=rating, **extra)


CARDS = {
    c.content_id: c
    for c in (
        make_card("orbit-run", "Orbit Run", ("sci-fi", "action"), moods=("tense",), minutes=44),
        make_card("star-forge", "Star Forge", ("sci-fi", "fantasy"), rating="TV-PG"),
        make_card("vector-zero", "Vector Zero", ("sci-fi", "action")),
        make_card("lantern", "Lantern Kingdom", ("animation", "fantasy"), categories=("Fantasy",)),
        make_card("glacier", "A Glacier's Year", ("documentary",), rating="TV-G"),
        make_card("diner", "Midnight Diner", ("drama", "comedy")),
        make_card("bunny", "Bunny Club", ("children",), rating="TV-Y"),
    )
}
CHANNEL = {
    "orbit-run": "starlight",
    "star-forge": "starlight",
    "vector-zero": "starlight",
    "lantern": "toon-harbor",
    "glacier": "wide-world",
    "diner": "laugh-lines",
}
CANDIDATES = [
    Candidate(content_id=i, channel=ch, start=NOW, end=NOW + timedelta(hours=1), score=1.0) for i, ch in CHANNEL.items()
]


def tile(content_id: str, reason: str, *evidence: str) -> dict[str, Any]:
    return {"content_id": content_id, "reason": reason, "evidence": list(evidence or ["declared_genre"])}


GOOD = [
    tile("orbit-run", "A tense sci-fi race, for your love of action"),
    tile("lantern", "Fantasy you like, on Toon Harbor"),
    tile("star-forge", "Sci-fi and fantasy in one quest"),
    tile("glacier", "Something different: a calm look at a glacier", "new_for_you"),
]


def check(tiles: list[dict[str, Any]], facts: Taste | Cohort = DANA, title: str = "For you", **kw: Any) -> Any:
    return validate({"title": title, "tiles": tiles}, candidates=CANDIDATES, cards=CARDS, facts=facts, **kw)


def test_a_good_rail_is_served_as_the_model_wrote_it() -> None:
    verdict = check(GOOD)

    assert verdict.valid
    assert verdict.title == "For you"
    assert [(t.content_id, t.reason, t.replaced) for t in verdict.tiles] == [
        (g["content_id"], g["reason"], False) for g in GOOD
    ]
    assert (verdict.faithful, verdict.checked) == (4, 4)
    assert not verdict.violations


def test_ids_outside_the_candidates_are_violations_and_dropped() -> None:
    verdict = check([tile("bunny", "Fun for you"), tile("no-such-title", "Great"), *GOOD])

    assert verdict.violations == {"not_candidate": 2}
    assert [t.content_id for t in verdict.tiles] == [g["content_id"] for g in GOOD]


@pytest.mark.parametrize(
    "reason",
    [
        "Like Vector Zero, but tenser",  # another title
        "A documentary for you",  # a genre the title lacks
        "Sci-fi on now, catch it",  # time-relative, stale within hours
        "Starts at 9:30 pm",  # a clock time
        "Three seasons of sci-fi action, 12 episodes",  # a number the card does not hold
        "Perfect for your evening",  # a daypart is not a taste fact
    ],
)
def test_unfaithful_reasons_are_replaced_by_their_evidence(reason: str) -> None:
    verdict = check([tile("orbit-run", reason), *GOOD[1:]])

    first = verdict.tiles[0]
    assert (first.content_id, first.replaced) == ("orbit-run", True)
    assert first.reason == "Because you like sci-fi"
    assert first.evidence == ("declared_genre",)
    assert verdict.dropped["unfaithful"] == 1
    assert verdict.faithful == 3


def test_facts_of_the_card_may_be_cited() -> None:
    verdict = check([tile("orbit-run", "Orbit Run: 44 minutes of tense sci-fi, rated TV-14"), *GOOD[1:]])
    assert not verdict.tiles[0].replaced


@pytest.mark.parametrize(
    ("reason", "forbidden"),
    [("Sponsored sci-fi for you", ()), ("Dana, you will love this", ("Dana",)), ("Mail dana@personas.example.com", ())],
)
def test_blocked_words_and_personal_data_are_violations(reason: str, forbidden: tuple[str, ...]) -> None:
    verdict = check([tile("orbit-run", reason), *GOOD[1:]], forbidden=forbidden)

    assert verdict.violations == {"blocked_or_personal": 1}
    assert verdict.tiles[0].reason == "Because you like sci-fi"


def test_a_bad_title_is_replaced() -> None:
    assert check(GOOD, title="Sponsored Picks").title == PERSONAL_TITLE
    assert check(GOOD, title="x" * 41).title == PERSONAL_TITLE
    assert check(GOOD, facts=EVENING, title="").title == COHORT_TITLE
    assert check(GOOD, title="Sponsored Picks").violations == {"title": 1}


def test_evidence_that_does_not_hold_is_dropped_or_derived() -> None:
    verdict = check([tile("glacier", "A calm glacier, something new", "declared_genre"), *GOOD[:3]])

    first = verdict.tiles[0]
    assert first.evidence == ("new_for_you",)
    assert first.replaced
    assert holds("new_for_you", CARDS["glacier"], DANA)
    assert not holds("daypart", CARDS["glacier"], DANA)


def test_picks_that_break_the_spread_move_below_the_top() -> None:
    # Four channels: at most two tiles of one channel among the first five.
    assert top_channel_limit(CANDIDATES) == 2
    picks = ["orbit-run", "vector-zero", "star-forge", "lantern", "glacier"]
    verdict = check([tile(i, "Sci-fi" if i != "glacier" else "A glacier", "new_for_you") for i in picks])

    assert [t.content_id for t in verdict.tiles] == ["orbit-run", "vector-zero", "lantern", "glacier", "star-forge"]
    assert verdict.dropped["moved"] == 3
    assert verdict.valid


def test_one_channel_may_fill_the_top_but_not_with_one_genre() -> None:
    one = [Candidate(content_id=c.content_id, channel="solo", start=NOW, end=NOW, score=1) for c in CANDIDATES]
    verdict = validate(
        {"title": "t", "tiles": [tile(i, "Sci-fi") for i in ("orbit-run", "vector-zero", "star-forge", "lantern")]},
        candidates=one,
        cards=CARDS,
        facts=DANA,
    )

    assert [t.content_id for t in verdict.tiles] == ["orbit-run", "vector-zero", "lantern", "star-forge"]


def test_long_rails_are_truncated_and_repeats_dropped() -> None:
    picks = [*CHANNEL, *CHANNEL, "orbit-run"]
    verdict = check([tile(i, "Sci-fi") for i in picks])

    assert len(verdict.tiles) == len(CHANNEL)
    assert verdict.dropped["duplicate"] == len(CHANNEL) + 1


def test_too_few_tiles_is_not_a_rail() -> None:
    assert not check(GOOD[:2]).valid


def test_cohort_reasons_may_name_their_daypart_only() -> None:
    cohort = [tile("glacier", "A calm documentary for the evening", "daypart")]
    good = check([*cohort, tile("lantern", "Fantasy tonight", "daypart"), tile("diner", "Comedy", "daypart")], EVENING)
    morning = check(cohort, Cohort(device="tv", language="en", daypart=Daypart.MORNING))

    assert [t.replaced for t in good.tiles] == [False, False, False]
    assert morning.tiles[0].replaced
    assert morning.tiles[0].reason == "Documentary for the morning"


def test_templates_are_faithful_and_short() -> None:
    for facts in (DANA, Taste(moods=("tense",)), Taste(genres=("romance",)), Taste(), EVENING):
        for content_id, channel in CHANNEL.items():
            reason, evidence = template(CARDS[content_id], channel, facts)
            assert len(reason) <= 90
            assert all(holds(code, CARDS[content_id], facts) for code in evidence)
            verdict = check([{"content_id": content_id, "reason": reason, "evidence": list(evidence)}], facts)
            assert verdict.faithful == verdict.checked == 1, (content_id, facts, reason)


def test_the_prompt_audit_finds_identifiers_and_commercial_words() -> None:
    assert audit_prompt('{"taste":{"genres":["sci-fi"]}}', ["vid-123", "Dana"]) == []
    assert audit_prompt('{"taste":{"genres":["sci-fi"]},"note":"Dana tier light"}', ["vid-123", "Dana"]) == [
        "Dana",
        "tier",
    ]
