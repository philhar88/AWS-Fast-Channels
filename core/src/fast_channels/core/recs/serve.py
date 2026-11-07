"""What ``GET /rail`` answers: a rail document, placed on the guide at request time.

A model rail is built once and served for hours, so it names titles, never times. At request time each tile is
placed at its title's soonest airing that has not ended (:func:`place`); tiles without one are dropped, and a rail
left with fewer than :data:`~.validate.MIN_TILES` tiles is served as the rule-based rail instead. The rule-based rail
(:func:`baseline`), the kids lineup (:func:`kids_lineup`) and the cohort rail re-ranked by declared genres
(:func:`seeded`) are made here too, with template reasons.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from fast_channels.core.recs.candidates import WINDOW, Cohort
from fast_channels.core.recs.rail import HORIZON, KIDS_CEILING, Airing, affinity, genre_weights, program_rail, within
from fast_channels.core.recs.router import Reason, Strategy
from fast_channels.core.recs.validate import COHORT_TITLE, PERSONAL_TITLE, RailTile

DOC_VERSION: Final = 1
COHORT_PREFIX: Final = "plan/rail/"
"""Where the cohort rails are, in the media bucket and on the CDN."""
BASELINE_TITLE: Final = "Up next for you"
KIDS_TITLE: Final = "For kids"
KIDS_LIMIT: Final = 8


class Lineage(BaseModel):
    """Where a model rail came from (DESIGN §15 step 6)."""

    model_config = ConfigDict(frozen=True)

    task: str
    model: str
    prompt_version: str
    schema_version: str
    catalog_version: str
    request_hash: str
    request_id: str
    built_at: AwareDatetime
    cost_usd: str
    latency_ms: int


class RailDoc(BaseModel):
    """A built rail: a viewer's model rail (``REQ#<hash>``) or a cohort rail (``plan/rail/<cohort>.json``)."""

    model_config = ConfigDict(frozen=True)

    v: int = DOC_VERSION
    strategy: Strategy
    title: str
    tiles: tuple[RailTile, ...]
    lineage: Lineage | None = None


class PlacedTile(BaseModel):
    """A tile as served: the title at its soonest airing."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    title: str
    channel: str
    program: str
    start: AwareDatetime
    end: AwareDatetime
    on_now: bool
    reason: str
    evidence: tuple[str, ...] = ()


class Answer(BaseModel):
    """The body of ``GET /rail``."""

    model_config = ConfigDict(frozen=True)

    strategy: Strategy
    reason: Reason
    title: str
    tiles: tuple[PlacedTile, ...]
    lineage: Lineage | None = None


def cohort_key(cohort: Cohort) -> str:
    """The object key of a cohort rail: ``plan/rail/<device>-<language>-<daypart>.json``."""
    return f"{COHORT_PREFIX}{cohort.key}.json"


def _soonest(guides: Iterable[Iterable[Airing]], now: datetime) -> dict[str, Airing]:
    soonest: dict[str, Airing] = {}
    for guide in guides:
        for airing in guide:
            if not airing.content_id or not airing.airs or airing.end <= now or airing.start >= now + WINDOW:
                continue
            best = soonest.get(airing.content_id)
            if best is None or airing.start < best.start:
                soonest[airing.content_id] = airing
    return soonest


def _placed(airing: Airing, now: datetime, reason: str, evidence: Sequence[str] = ()) -> PlacedTile:
    return PlacedTile(
        content_id=airing.content_id or airing.name,
        title=airing.title,
        channel=airing.channel,
        program=airing.name,
        start=airing.start,
        end=airing.end,
        on_now=airing.start <= now,
        reason=reason,
        evidence=tuple(evidence),
    )


def place(tiles: Iterable[RailTile], guides: Iterable[Iterable[Airing]], now: datetime) -> list[PlacedTile]:
    """``tiles`` at their titles' soonest airings that have not ended, in the rail's order; tiles whose title no
    longer airs in the next :data:`~.candidates.WINDOW` are dropped."""
    soonest = _soonest(guides, now)
    return [_placed(soonest[t.content_id], now, t.reason, t.evidence) for t in tiles if t.content_id in soonest]


def baseline(genres: Sequence[str], guides: Sequence[Sequence[Airing]], now: datetime) -> list[PlacedTile]:
    """The rule-based rail (:func:`~.rail.program_rail`) with its reasons."""
    tiles = []
    evidence: tuple[str, ...]
    for tile in program_rail(genres, guides, now):
        if tile.matched:
            reason, evidence = f"You like {' and '.join(tile.matched)}", ("declared_genre",)
        else:
            reason, evidence = f"Coming up on {tile.airing.channel}", ()
        tiles.append(_placed(tile.airing, now, reason, evidence))
    return tiles


def kids_lineup(
    guides: Iterable[Iterable[Airing]], now: datetime, *, child_directed: Collection[str] = ()
) -> list[PlacedTile]:
    """The kids lineup: titles of child-directed channels and titles rated for children, on now or within
    :data:`~.rail.HORIZON`, soonest first, each once. No model, no viewer data."""
    seen: set[str] = set()
    tiles = []
    airings = sorted(
        (a for guide in guides for a in guide if a.airs and a.end > now and a.start < now + HORIZON),
        key=lambda a: a.start,
    )
    for airing in airings:
        if airing.channel not in child_directed and not within(airing.rating, KIDS_CEILING):
            continue
        if (key := airing.title.lower()) not in seen:
            seen.add(key)
            tiles.append(_placed(airing, now, f"For kids on {airing.channel}"))
    return tiles[:KIDS_LIMIT]


def seeded(
    tiles: Sequence[PlacedTile], genres: Sequence[str], genres_of: Mapping[str, Sequence[str]]
) -> list[PlacedTile]:
    """A cohort rail re-ranked by declared ``genres`` (an account's first session): the titles that match first, in
    the cohort's order otherwise. ``genres_of`` gives each title's genres."""
    weights = genre_weights(genres)
    return sorted(tiles, key=lambda t: -affinity(genres_of.get(t.content_id, ()), weights)[0])


def answer(
    strategy: Strategy,
    reason: Reason,
    tiles: Sequence[PlacedTile],
    *,
    title: str | None = None,
    lineage: Lineage | None = None,
) -> Answer:
    """The body of a ``GET /rail`` response."""
    default = {
        Strategy.LLM: PERSONAL_TITLE,
        Strategy.COHORT: COHORT_TITLE,
        Strategy.BASELINE: BASELINE_TITLE,
        Strategy.KIDS: KIDS_TITLE,
    }[strategy]
    return Answer(strategy=strategy, reason=reason, title=title or default, tiles=tuple(tiles), lineage=lineage)
