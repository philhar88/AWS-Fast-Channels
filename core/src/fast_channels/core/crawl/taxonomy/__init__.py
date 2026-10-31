"""IAB Tech Lab Content Taxonomy 3.1 ids and the catalog genre map (IAB CTV Genre Mapping); see ``ATTRIBUTION.md``.

``ct31-ids.txt`` lists every Content Taxonomy 3.1 id with its name. ``genre-map.toml`` maps the catalog's lower-case
genre tags to CTV genres and each CTV genre to its 3.1 id. :func:`load_genre_map` refuses a map that names an id outside
the taxonomy, so every id the context object carries (``genres`` with ``gtax`` 9, ``cat`` with ``cattax`` 9) is a
Content Taxonomy 3.1 id.
"""

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

TAXONOMY_DIR: Final = Path(__file__).parent
CT31_IDS: Final = TAXONOMY_DIR / "ct31-ids.txt"
GENRE_MAP: Final = TAXONOMY_DIR / "genre-map.toml"
CT31: Final = 9
"""AdCOM Category Taxonomies: 9 is IAB Tech Lab Content Taxonomy 3.1. OpenRTB's default ``cattax``, 1, means Content
Taxonomy 1.0, so ``cattax`` and ``gtax`` are always sent explicitly."""
MAX_GENRES: Final = 3
"""Genres per title, most apt first."""


@dataclass(frozen=True, slots=True)
class Genre:
    """A Content Taxonomy 3.1 entry a catalog genre tag maps to."""

    id: str
    """Unique id, a string (some ids are alphanumeric, such as ``KHPC5A``)."""
    name: str
    """Name in Content Taxonomy 3.1, such as ``Science Fiction``."""


@dataclass(frozen=True, slots=True)
class Resolved:
    """A title's genre tags, split into Content Taxonomy 3.1 genres and the remaining tags."""

    genres: tuple[Genre, ...]
    """Mapped genres in catalog order, without repeated ids, at most :data:`MAX_GENRES`."""
    other_tags: tuple[str, ...]
    """Tags with no CTV genre, in catalog order and without repeats: descriptive keywords, not genres."""


class _GenreMapFile(BaseModel):
    """Schema of ``genre-map.toml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ctv: dict[str, str]
    """CTV genre → Content Taxonomy 3.1 id."""
    tags: dict[str, str]
    """Lower-case catalog genre tag → CTV genre."""


class GenreMap:
    """Catalog genre tags → Content Taxonomy 3.1 genres."""

    def __init__(self, tags: Mapping[str, Genre]) -> None:
        self._tags = dict(tags)

    @property
    def tags(self) -> Mapping[str, Genre]:
        """Every mapped tag and its genre."""
        return self._tags

    def resolve(self, tags: Iterable[str]) -> Resolved:
        """Split ``tags`` into genres and other tags; tags are compared lower case, without surrounding spaces."""
        genres: list[Genre] = []
        other: list[str] = []
        for tag in filter(None, map(_norm, tags)):
            match self._tags.get(tag):
                case None:
                    if tag not in other:
                        other.append(tag)
                case genre if genre not in genres and len(genres) < MAX_GENRES:
                    genres.append(genre)
                case _:
                    pass  # a repeated id, or a genre beyond the most apt three
        return Resolved(tuple(genres), tuple(other))


def load_taxonomy(path: Path = CT31_IDS) -> dict[str, str]:
    """Content Taxonomy 3.1 names by id from ``path`` (``<id><TAB><name>`` lines; ``#`` starts a comment line)."""
    names: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        match line.split("\t"):
            case [id_, name] if id_ and name and id_ not in names:
                names[id_] = name
            case [id_, _] if id_ in names:
                raise ValueError(f"{path.name}:{number}: id {id_} is listed twice")
            case _:
                raise ValueError(f"{path.name}:{number}: expected '<id><TAB><name>', got {line!r}")
    return names


def load_genre_map(path: Path = GENRE_MAP, *, taxonomy: Mapping[str, str] | None = None) -> GenreMap:
    """Read and check a genre map: every id must be in ``taxonomy`` (default: ``ct31-ids.txt``), every tag must be
    lower case and name a CTV genre of the map.

    Raises:
        ValueError: listing every problem found.
    """
    names = load_taxonomy() if taxonomy is None else taxonomy
    document = _GenreMapFile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    problems = [
        f"CTV genre {ctv!r} maps to {id_!r}, which is not a Content Taxonomy 3.1 id"
        for ctv, id_ in document.ctv.items()
        if id_ not in names
    ]
    problems += [
        f"tag {tag!r} must be lower case without surrounding spaces" for tag in document.tags if tag != _norm(tag)
    ]
    problems += [
        f"tag {tag!r} names {ctv!r}, which is not a CTV genre of the map"
        for tag, ctv in document.tags.items()
        if ctv not in document.ctv
    ]
    if problems:
        raise ValueError(f"{path.name}: " + "; ".join(problems))
    return GenreMap(
        {tag: Genre(document.ctv[ctv], names[document.ctv[ctv]]) for tag, ctv in document.tags.items()},
    )


@cache
def default_genre_map() -> GenreMap:
    """The shipped genre map, loaded once per process."""
    return load_genre_map()


def _norm(tag: str) -> str:
    return tag.strip().lower()
