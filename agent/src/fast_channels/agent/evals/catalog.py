"""Deterministic 12-title reference catalog with natural break points (from the v3 model benchmark)."""

import random
from dataclasses import dataclass
from functools import cache
from typing import Final

FAMILY_RATINGS: Final = frozenset({"TV-Y", "TV-Y7", "TV-G", "TV-PG"})
LADDER_VERSION: Final = "standard@eval000001"

_TITLES: Final = (
    ("ff-001", "The Lighthouse Keeper's Cat", "Animation", "TV-Y7", 1_351_480),
    ("ff-002", "Ocean Giants: The Great Migration", "Documentary", "TV-G", 3_118_760),
    ("ff-003", "Midnight Heist", "Thriller", "TV-MA", 5_882_040),
    ("ff-004", "Garden Gnome Rescue", "Animation", "TV-Y", 658_960),
    ("ff-005", "Space Cadets: First Contact", "Sci-Fi", "TV-PG", 2_641_920),
    ("ff-006", "Kitchen Science Lab", "Education", "TV-G", 1_679_360),
    ("ff-007", "Storm Chasers Unleashed", "Reality", "TV-14", 2_519_040),
    ("ff-008", "The Great Bake Along", "Cooking", "TV-G", 2_762_880),
    ("ff-009", "Robo Pals: The Movie", "Animation", "TV-PG", 5_279_520),
    ("ff-010", "City Nights", "Drama", "TV-14", 3_302_400),
    ("ff-011", "Wild Rivers of Patagonia", "Documentary", "TV-G", 3_509_880),
    ("ff-012", "Puppet Parade", "Kids", "TV-Y", 900_480),
)


@dataclass(frozen=True, slots=True)
class Title:
    """One catalog title and its encoded asset on the eval ladder."""

    content_id: str
    title: str
    genre: str
    rating: str
    duration_ms: int
    break_offsets_ms: tuple[int, ...]

    @property
    def asset_id(self) -> str:
        return f"{self.content_id}-a1b2c3"


@cache
def titles() -> tuple[Title, ...]:
    """The catalog; break points are scene changes every ~2.3-4.3 min, never in the first or last minute."""
    rng = random.Random(20261015)  # noqa: S311 - deterministic fixture data, not security
    return tuple(
        Title(content_id, title, genre, rating, duration, _break_points(rng, duration))
        for content_id, title, genre, rating, duration in _TITLES
    )


def by_asset() -> dict[str, Title]:
    """Titles keyed by asset id."""
    return {title.asset_id: title for title in titles()}


def _break_points(rng: random.Random, duration_ms: int) -> tuple[int, ...]:
    points, t = [], 60_000 + rng.randint(0, 90_000)
    while t < duration_ms - 60_000:
        points.append(t)
        t += rng.randint(140_000, 260_000) // 40 * 40  # 25 fps frame aligned
    return tuple(points)
