"""The catalog card of a title: what the model is told about it, and the facts its reasons may cite.

A card joins the CMS's catalog item (``ReferenceCatalog`` ``CONTENT#<id>`` / ``META``) with the content understanding
module's item beside it (``CONTENT#<id>`` / ``UNDERSTANDING``, ``contracts/content-understanding.schema.json``) when
that exists and its model step ran: IAB Content Taxonomy 3.1 categories, moods and keywords. Without it, the categories
come from the catalog genres through the crawl module's genre map. The CMS's synopsis and language win over the
understood ones.

Catalog text is untrusted input: it reaches the model only as quoted JSON, cut to fixed lengths, and the model's
output is checked against the card's facts, never trusted for them.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from functools import cache
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from fast_channels.core.crawl.taxonomy import GenreMap, default_genre_map, load_taxonomy
from fast_channels.core.understanding.record import MOODS
from fast_channels.core.understanding.record import VERSION as UNDERSTANDING_VERSION

SYNOPSIS_MAX: Final = 300
TITLE_MAX: Final = 80
MAX_CATEGORIES: Final = 5
MAX_MOODS: Final = 3
MAX_KEYWORDS: Final = 10
_WORD: Final = re.compile(r"^[a-z][a-z' -]{0,29}$")
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]+")


class Card(BaseModel):
    """One title's card."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    title: str
    synopsis: str = ""
    genres: tuple[str, ...] = ()
    """Lower-case catalog genre tags."""
    categories: tuple[str, ...] = ()
    """Content Taxonomy 3.1 names, most apt first."""
    moods: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    rating: str | None = None
    minutes: int | None = None
    language: str | None = None
    series: str | None = None

    def prompt_fields(self) -> dict[str, Any]:
        """The card as the catalog block shows it: empty fields left out."""
        fields = {
            "id": self.content_id,
            "title": self.title,
            "series": self.series,
            "synopsis": self.synopsis,
            "genres": list(self.genres),
            "categories": list(self.categories),
            "moods": list(self.moods),
            "keywords": list(self.keywords),
            "rating": self.rating,
            "minutes": self.minutes,
            "language": self.language,
        }
        return {key: value for key, value in fields.items() if value not in (None, "", [])}


@cache
def _taxonomy() -> dict[str, str]:
    return load_taxonomy()


def _text(value: object, limit: int) -> str:
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


def _words(values: object, limit: int) -> tuple[str, ...]:
    """Lower-case words or short phrases, without repeats; anything else (instructions, say) is dropped."""
    if not isinstance(values, list | tuple):
        return ()
    words: list[str] = []
    for value in values:
        word = _text(value, 200).lower()  # longer than _WORD allows: dropped, never cut to fit
        if _WORD.fullmatch(word) and word not in words:
            words.append(word)
    return tuple(words[:limit])


def card(
    meta: Mapping[str, Any],
    understanding: Mapping[str, Any] | None = None,
    *,
    taxonomy: Mapping[str, str] | None = None,
    genre_map: GenreMap | None = None,
) -> Card:
    """The card of the catalog item ``meta``, with ``understanding`` (its ``UNDERSTANDING`` item) if there is one.

    An understanding item counts only at schema version 1 and when its model step ran (``model`` not null); its
    category ids are resolved through Content Taxonomy 3.1, and unknown ones are dropped.
    """
    names = _taxonomy() if taxonomy is None else taxonomy
    genres = _words(meta.get("genres"), 10)
    understood = (
        understanding
        if understanding
        and understanding.get("v", UNDERSTANDING_VERSION) == UNDERSTANDING_VERSION
        and understanding.get("model")
        else {}
    )
    categories = [names[str(i)] for i in understood.get("cat") or () if str(i) in names]
    if not categories:
        categories = [genre.name for genre in (genre_map or default_genre_map()).resolve(genres).genres]
    duration_ms = meta.get("duration_ms")
    minutes = (  # DynamoDB numbers are Decimals
        round(int(duration_ms) / 60_000) if isinstance(duration_ms, int | float | Decimal) and duration_ms > 0 else None
    )
    keywords = [*_words(meta.get("keywords"), MAX_KEYWORDS), *_words(understood.get("keywords"), MAX_KEYWORDS)]
    return Card(
        content_id=str(meta["content_id"]),
        title=_text(meta.get("title"), TITLE_MAX) or str(meta["content_id"]),
        synopsis=_text(meta.get("synopsis") or understood.get("synopsis"), SYNOPSIS_MAX),
        genres=genres,
        categories=tuple(dict.fromkeys(categories))[:MAX_CATEGORIES],
        moods=tuple(m for m in _words(understood.get("moods"), MAX_KEYWORDS) if m in MOODS)[:MAX_MOODS],
        keywords=tuple(dict.fromkeys(keywords))[:MAX_KEYWORDS],
        rating=_text(meta.get("rating"), 10) or None,
        minutes=minutes or None,
        language=_text(meta.get("language") or understood.get("language"), 8) or None,
        series=_text(meta.get("series_title"), TITLE_MAX) or None,
    )


def catalog_version(cards: Iterable[Card]) -> str:
    """A digest of the card set: identical catalogs give identical prompts, so they share a cached prefix and
    request hashes."""
    document = json.dumps(
        [c.model_dump(mode="json") for c in sorted(cards, key=lambda c: c.content_id)],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode()).hexdigest()[:16]


def by_id(cards: Sequence[Card]) -> dict[str, Card]:
    """``cards`` by content id."""
    return {c.content_id: c for c in cards}
