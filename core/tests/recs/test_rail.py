import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.recs.rail import Airing, Daypart, airings, genre_weights, program_rail, rating_rank, within

FIXTURE = Path(__file__).resolve().parents[3] / "contracts" / "fixtures" / "rail" / "program-rail.json"
SHARED: dict[str, Any] = json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("case", SHARED["cases"], ids=[c["name"] for c in SHARED["cases"]])
def test_the_rule_based_rail_matches_the_viewer(case: dict[str, Any]) -> None:
    """The same fixture runs in console/src/viewer/rail.test.ts against programRail."""
    guides = [airings(channel, {"programs": SHARED["epg"][channel]}) for channel in case["guides"]]
    now = datetime.fromisoformat(case["now"])

    rail = program_rail(case["genres"], guides, now, **({"limit": case["limit"]} if "limit" in case else {}))

    assert [{"channel": t.airing.channel, "name": t.airing.name} for t in rail] == case["expected"]


def test_genre_weights_fall_with_rank_and_keep_a_repeated_genre_in_its_first_place() -> None:
    assert genre_weights(["Sci-Fi", "action", "fantasy"]) == {"sci-fi": 1, "action": 2 / 3, "fantasy": 1 / 2}
    assert list(genre_weights(["sci-fi", "action", "sci-fi"]).items()) == [("sci-fi", 1 / 2), ("action", 2 / 3)]


def test_ratings_and_ceilings() -> None:
    assert within("TV-PG", "TV-14")
    assert within("PG-13", "TV-14")  # a film rating by its TV equivalent
    assert not within("TV-MA", "TV-14")
    assert not within(None, "TV-MA")  # unrated is above every ceiling
    assert not within("NC-17", "TV-MA")
    assert rating_rank("tv-y") == 0


@pytest.mark.parametrize(
    ("hour", "daypart"),
    [(6, Daypart.MORNING), (11, Daypart.MORNING), (12, Daypart.AFTERNOON), (18, Daypart.EVENING), (23, Daypart.NIGHT),
     (2, Daypart.NIGHT)],
)  # fmt: skip
def test_dayparts_are_utc(hour: int, daypart: Daypart) -> None:
    assert Daypart.at(datetime(2026, 11, 10, hour, 30, tzinfo=UTC)) is daypart


def test_skipped_programs_do_not_air() -> None:
    start, end = datetime(2026, 11, 10, 20, tzinfo=UTC), datetime(2026, 11, 10, 21, tzinfo=UTC)
    skipped = Airing(channel="c", name="p", title="T", start=start, end=end, status="SKIPPED")
    assert not skipped.airs
