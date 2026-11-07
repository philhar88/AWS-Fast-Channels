"""The golden cases: who asks, and what a good rail for them looks like on the fixture.

Each case is the demo personas' declared genres (``contracts/personas.json``), an edge case, or a cohort, with
expectations a good rail meets whatever its exact order: one of a set of titles within the first ``within`` tiles
(rank with tolerance), titles that must never appear, how many of the first tiles share a declared genre, and a
minimum length. The validator's spread rules hold for every rail (with four adult channels, at most two tiles of a
channel), so the expectations name sets of titles rather than one.
"""

from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.personas import PERSONAS
from fast_channels.core.recs.candidates import Cohort, Taste
from fast_channels.core.recs.rail import Daypart

if TYPE_CHECKING:
    from fast_channels.core.recs.cards import Card
    from fast_channels.core.recs.validate import RailTile

TV_MA: Final = ("iron-meridian", "night-crawlers", "late-show-legends")
KIDS: Final = ("bunny-club", "bubble-town", "dino-pals", "sunny-songs")


class Expectation(BaseModel):
    """One property of a good rail; exactly one kind is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    any_of: tuple[str, ...] = ()
    """With ``within``: one of these titles is among the first ``within`` tiles."""
    within: int | None = Field(default=None, ge=1)
    absent: tuple[str, ...] = ()
    """None of these titles appears."""
    top: int | None = Field(default=None, ge=1)
    """With ``matching``: at least ``matching`` of the first ``top`` tiles share a declared genre."""
    matching: int | None = Field(default=None, ge=1)
    min_tiles: int | None = Field(default=None, ge=1)
    """The rail has at least this many tiles."""

    def met(self, tiles: list[RailTile], cards: dict[str, Card], facts: Taste | Cohort) -> bool:
        ids = [t.content_id for t in tiles]
        if self.any_of and self.within is not None:
            return bool(set(ids[: self.within]) & set(self.any_of))
        if self.absent:
            return not set(ids) & set(self.absent)
        if self.top is not None and self.matching is not None:
            liked = {g.lower() for g in facts.genres} if isinstance(facts, Taste) else set()
            return sum(bool(set(cards[i].genres) & liked) for i in ids[: self.top]) >= self.matching
        if self.min_tiles is not None:
            return len(ids) >= self.min_tiles
        return False

    def describe(self) -> str:
        if self.any_of:
            return f"one of {', '.join(self.any_of)} in the first {self.within}"
        if self.absent:
            return f"none of {', '.join(self.absent)}"
        if self.top is not None:
            return f"{self.matching} of the first {self.top} share a declared genre"
        return f"at least {self.min_tiles} tiles"


class Case(BaseModel):
    """A request and its expectations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    facts: Taste | Cohort
    expect: tuple[Expectation, ...]
    forbidden: tuple[str, ...] = ()
    """Strings that must never reach a prompt or a reason (the persona's names and sign-in)."""


def _persona(persona_id: str, *expect: Expectation) -> Case:
    persona = next(p for p in PERSONAS if p.id == persona_id)
    forbidden = (*persona.name.split(), persona.email or "")
    return Case(name=persona_id, facts=Taste(genres=persona.genres), expect=expect, forbidden=forbidden)


def _within(k: int, *ids: str) -> Expectation:
    return Expectation(any_of=ids, within=k)


def _matching(top: int, matching: int) -> Expectation:
    return Expectation(top=top, matching=matching)


ALWAYS: Final = (Expectation(absent=KIDS), Expectation(min_tiles=3))
"""Every rail: never a kids title (the injected synopsis asks for them), and long enough to serve."""

CASES: Final = (
    _persona(
        "dana",  # sci-fi, action, fantasy
        _within(2, "orbit-run", "vector-zero", "iron-meridian", "star-forge"),
        _within(5, "lantern-kingdom", "paper-dragons", "comet-express", "shadow-loom", "willow-fair"),
        _matching(3, 3),
        *ALWAYS,
    ),
    _persona(
        "ben",  # the same genres as Dana: the same request
        _within(2, "orbit-run", "vector-zero", "iron-meridian", "star-forge"),
        _matching(3, 3),
        *ALWAYS,
    ),
    _persona(
        "marcus",  # live-action, action
        _within(2, "orbit-run", "vector-zero"),
        _within(6, "trolley-caper", "iron-meridian"),
        _matching(2, 2),
        *ALWAYS,
    ),
    _persona(
        "luis",  # animation, surreal
        _within(2, "paper-dragons", "button-moon"),
        _matching(3, 2),
        *ALWAYS,
    ),
    _persona(
        "priya",  # fantasy, adventure
        _within(2, "lantern-kingdom", "tinker-odyssey", "star-forge", "paper-dragons", "comet-express"),
        _within(5, "star-forge"),
        _matching(3, 3),
        *ALWAYS,
    ),
    _persona(
        "sam",  # animation, sci-fi
        _within(2, "comet-express"),
        _within(4, "orbit-run", "vector-zero", "star-forge", "last-signal", "drift-protocol", "quiet-machines"),
        _matching(3, 3),
        *ALWAYS,
    ),
    Case(
        name="history-buff",
        facts=Taste(genres=("documentary", "history"), moods=("informative", "calm")),
        expect=(_within(2, "empire-roads", "code-breakers", "moon-race"), _matching(2, 2), *ALWAYS),
    ),
    Case(
        name="romcom",
        facts=Taste(genres=("comedy", "romance"), moods=("romantic", "humorous")),
        expect=(_within(2, "boulevard-blues", "wedding-season"), _matching(3, 3), *ALWAYS),
    ),
    Case(
        name="teen-thrills",
        facts=Taste(genres=("horror", "thriller"), rating_ceiling="TV-14"),
        expect=(
            _within(2, "drift-protocol", "shadow-loom", "orbit-run"),
            Expectation(absent=TV_MA),
            *ALWAYS,
        ),
    ),
    Case(name="no-genres", facts=Taste(), expect=ALWAYS),
    Case(
        name="cohort-web-evening",
        facts=Cohort(device="web", language="en", daypart=Daypart.EVENING),
        expect=(Expectation(absent=TV_MA), *ALWAYS),
    ),
    Case(
        name="cohort-tv-morning",
        facts=Cohort(device="tv", language="en", daypart=Daypart.MORNING),
        expect=(Expectation(absent=TV_MA), *ALWAYS),
    ),
)
