"""Guide airings, content ratings, dayparts, and the rule-based rail.

:func:`program_rail` is a port of ``programRail`` in ``console/src/viewer/rail.ts`` (DESIGN §15's genre baseline):
programs on now or starting within :data:`HORIZON`, scored by a persona's declared genres, best first, then soonest,
each title once. It is the fallback of every model rail, so the viewer shows the same rail whether it ranks locally
or the recommender answers with its fallback; ``contracts/fixtures/rail/program-rail.json`` keeps the two identical.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

HORIZON: Final = timedelta(hours=3)
"""How far ahead the rule-based rail looks (``HORIZON_MS`` in rail.ts)."""
BASELINE_LIMIT: Final = 8
"""Tiles of the rule-based rail (``programRail``'s default ``limit``)."""
RATINGS: Final = ("TV-Y", "TV-Y7", "TV-G", "TV-PG", "TV-14", "TV-MA")
"""US TV Parental Guidelines, mildest first."""
_FILM_RATINGS: Final = {"G": "TV-G", "PG": "TV-PG", "PG-13": "TV-14", "R": "TV-MA"}
"""MPA film ratings by their TV equivalent; NC-17 has none, so it is above every ceiling."""
KIDS_CEILING: Final = "TV-G"
"""The rating ceiling of a kids lineup."""
COHORT_CEILING: Final = "TV-14"
"""The rating ceiling of a cohort rail: its viewers are not known to be adults."""
ADULT_CEILING: Final = "TV-MA"
"""The rating ceiling of a signed-in adult."""


def rating_rank(rating: str | None) -> int:
    """Position of ``rating`` in :data:`RATINGS`; an unknown or missing rating ranks above every ceiling."""
    normalized = (rating or "").strip().upper()
    normalized = _FILM_RATINGS.get(normalized, normalized)
    return RATINGS.index(normalized) if normalized in RATINGS else len(RATINGS)


def within(rating: str | None, ceiling: str) -> bool:
    """Whether a title rated ``rating`` may be shown under ``ceiling``."""
    return rating_rank(rating) <= rating_rank(ceiling)


class Daypart(StrEnum):
    """The four UTC dayparts of a cohort rail."""

    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"

    @classmethod
    def at(cls, moment: datetime) -> Self:
        """The daypart of ``moment`` (UTC): morning from 06:00, afternoon from 12:00, evening from 18:00, night from
        23:00."""
        hour = moment.astimezone(UTC).hour
        match hour:
            case h if 6 <= h < 12:
                return cls.MORNING
            case h if 12 <= h < 18:
                return cls.AFTERNOON
            case h if 18 <= h < 23:
                return cls.EVENING
            case _:
                return cls.NIGHT


class Airing(BaseModel):
    """One program of a channel's guide (``epg/<channel>.json``), as the recommender reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    channel: str
    name: str = Field(description="Channel Assembly program name.")
    content_id: str | None = None
    title: str
    start: AwareDatetime
    end: AwareDatetime
    rating: str | None = None
    genres: tuple[str, ...] = ()
    status: str = "PUBLISHED"

    @property
    def airs(self) -> bool:
        """Whether the program airs: ``SKIPPED`` ones were removed unaired."""
        return self.status != "SKIPPED"


def airings(channel: str, epg: Mapping[str, Any]) -> list[Airing]:
    """The programs of ``channel``'s guide document, in guide order."""
    return [Airing.model_validate({**program, "channel": channel}) for program in epg.get("programs", [])]


def genre_weights(genres: Iterable[str]) -> dict[str, float]:
    """Each declared genre's weight: 1 for the first, falling with rank (1, 2/3, 1/2, 2/5, …)."""
    weights: dict[str, float] = {}
    for rank, genre in enumerate(genres):
        weights[genre.lower()] = 1 / (1 + rank / 2)  # a repeated genre keeps its place and its last weight (a JS Map)
    return weights


def affinity(genres: Iterable[str], weights: Mapping[str, float]) -> tuple[float, list[str]]:
    """The affinity of a program with ``genres`` and the declared genres it matched, in the declared order."""
    present = {genre.lower() for genre in genres}
    matched = [genre for genre in weights if genre in present]
    return sum(weights[genre] for genre in matched), matched


class BaselineTile(BaseModel):
    """A tile of the rule-based rail."""

    model_config = ConfigDict(frozen=True)

    airing: Airing
    on_now: bool
    score: float
    matched: tuple[str, ...]


def program_rail(
    genres: Sequence[str], guides: Sequence[Sequence[Airing]], now: datetime, limit: int = BASELINE_LIMIT
) -> list[BaselineTile]:
    """Programs on now or starting within :data:`HORIZON` that match ``genres``, best match first, then soonest:
    each title once, at its first airing in that order. Without genres (a guest), every program in the window, soonest
    first. ``guides`` are the channels' airings in the viewer's channel order, which breaks ties."""
    weights = genre_weights(genres)
    tiles: list[BaselineTile] = []
    for guide in guides:
        for airing in guide:
            if not airing.airs or airing.end <= now or airing.start >= now + HORIZON:
                continue
            score, matched = affinity(airing.genres, weights)
            tiles.append(BaselineTile(airing=airing, on_now=airing.start <= now, score=score, matched=tuple(matched)))
    ranked = sorted(
        (tile for tile in tiles if not weights or tile.score > 0),
        key=lambda tile: (-tile.score, tile.airing.start),
    )
    seen: set[str] = set()
    rail: list[BaselineTile] = []
    for tile in ranked:
        title = tile.airing.title.lower()
        if title not in seen:
            seen.add(title)
            rail.append(tile)
    return rail[:limit]
