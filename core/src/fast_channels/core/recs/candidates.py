"""The titles a model may choose among: the next 24 hours of the guides, filtered by policy and scored.

Policy comes first and is never the model's: titles above the rating ceiling, titles of child-directed channels and
airings that will not air are not candidates, so the model cannot pick them (the validator checks again). The score is
the rule-based rail's genre affinity, plus shared moods and a bonus for titles on within :data:`~.rail.HORIZON`; it
orders the candidates the model sees and keeps the best :data:`TOP_K`.

What the model learns about the viewer is :class:`Taste`: taste facts only, never an identifier, a name or anything
commercial. A cohort rail has a :class:`Cohort` instead and no viewer data at all.
"""

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.recs.rail import (
    ADULT_CEILING,
    COHORT_CEILING,
    HORIZON,
    Airing,
    Daypart,
    affinity,
    genre_weights,
    within,
)

if TYPE_CHECKING:
    from fast_channels.core.recs.cards import Card

TOP_K: Final = 40
WINDOW: Final = timedelta(hours=24)
MOOD_WEIGHT: Final = 0.5
SOON_BONUS: Final = 0.25


class Taste(BaseModel):
    """What the model is told about a viewer: declared or learned genres and moods, most liked first."""

    model_config = ConfigDict(frozen=True)

    genres: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    rating_ceiling: str = ADULT_CEILING


class Cohort(BaseModel):
    """A cohort rail's audience: no viewer data."""

    model_config = ConfigDict(frozen=True)

    device: str = Field(pattern=r"^[a-z]{2,10}$")
    language: str = Field(pattern=r"^[a-z]{2}$")
    daypart: Daypart

    @property
    def key(self) -> str:
        """``<device>-<language>-<daypart>``, the cohort rail's file name."""
        return f"{self.device}-{self.language}-{self.daypart}"

    @property
    def rating_ceiling(self) -> str:
        return COHORT_CEILING


class Candidate(BaseModel):
    """A title the model may pick, at its soonest airing in the window."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    channel: str
    start: datetime
    end: datetime
    score: float

    def prompt_fields(self) -> dict[str, object]:
        """The candidate as the request block shows it: no times, because a rail is served for hours."""
        return {"id": self.content_id, "channel": self.channel, "score": round(self.score, 2)}


def score(card: Card, taste: Taste | None, *, soon: bool) -> float:
    """A title's candidate score for ``taste`` (``None``: a cohort)."""
    weights = genre_weights(taste.genres if taste else ())
    genre_score, _ = affinity(card.genres, weights)
    moods = set(taste.moods if taste else ())
    mood_score = MOOD_WEIGHT * len(moods & set(card.moods)) / len(moods) if moods else 0.0
    return genre_score + mood_score + (SOON_BONUS if soon else 0.0)


def candidates(
    cards: Mapping[str, Card],
    guides: Iterable[Iterable[Airing]],
    now: datetime,
    facts: Taste | Cohort,
    *,
    child_directed: Collection[str] = (),
    k: int = TOP_K,
) -> list[Candidate]:
    """The best ``k`` titles airing in the next :data:`WINDOW` for ``facts``: each title once, at its soonest airing;
    titles without a card, above the rating ceiling or on a ``child_directed`` channel are left out."""
    soonest: dict[str, Airing] = {}
    for guide in guides:
        for airing in guide:
            if not airing.airs or airing.end <= now or airing.start >= now + WINDOW:
                continue
            content_id = airing.content_id
            if content_id is None or content_id not in cards or airing.channel in child_directed:
                continue
            if not within(cards[content_id].rating, facts.rating_ceiling):
                continue
            best = soonest.get(content_id)
            if best is None or airing.start < best.start:
                soonest[content_id] = airing
    taste = facts if isinstance(facts, Taste) else None
    scored = [
        Candidate(
            content_id=content_id,
            channel=airing.channel,
            start=airing.start,
            end=airing.end,
            score=score(cards[content_id], taste, soon=airing.start < now + HORIZON),
        )
        for content_id, airing in soonest.items()
    ]
    return sorted(scored, key=lambda c: (-c.score, c.start, c.content_id))[:k]


def catalog_cards(
    cards: Mapping[str, Card],
    guides: Iterable[Iterable[Airing]],
    now: datetime,
    *,
    ceiling: str,
    child_directed: Collection[str] = (),
) -> list[Card]:
    """The cards of a catalog block: every title airing in the next :data:`WINDOW` within the rating ``ceiling`` on a
    channel that is not child-directed, in content id order. A request never sees a title it may not be shown, so it
    cannot pick one: kids titles never enter the prompt (a kids lineup uses no model), and a day has one catalog block
    per ceiling (adults, and cohorts), each cached."""
    airing: set[str] = {
        a.content_id
        for guide in guides
        for a in guide
        if a.content_id and a.airs and a.end > now and a.start < now + WINDOW and a.channel not in child_directed
    }
    return [cards[i] for i in sorted(airing & cards.keys()) if within(cards[i].rating, ceiling)]
