"""The catalog context object: one title's show-level metadata in the shapes ad requests use.

The context publisher writes one object per title to the media bucket at :func:`context_key`, served through the CDN.
A cached PRE_ADS ``HTTP_REQUEST`` Function reads it by the break's SCTE-35 UPID content id and forwards ``hdr`` as a
header; the Pod Decision Service reads the same object into OpenRTB ``app.content``. Every value comes from the
catalog and the publisher's own domain: the object holds no session, player or viewer data.

The schema of ``v1`` never changes in place: MediaTailor cache entries cannot be invalidated, so a new schema ships as
``v2`` under a new prefix with a new Function cache key (``contracts/catalog-context.schema.json`` pins ``v1``).
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Final, Literal, Self
from urllib.parse import quote

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.catalog import CONTENT_ID_PATTERN, ContentItem, ContentType, is_domain
from fast_channels.core.crawl.taxonomy import CT31, MAX_GENRES, GenreMap

CONTEXT_VERSION: Final = 1
CONTEXT_PREFIX: Final = f"catalog/ctx/v{CONTEXT_VERSION}/"
"""Key prefix of the context objects in the media bucket (and their path on the CDN)."""
CACHE_CONTROL: Final = "max-age=300"
"""``Cache-Control`` of every object: the PRE_ADS Function caches a response for 300 s, the CDN as long."""
OUTPUT_MAX_CHARS: Final = 1_000
"""MediaTailor Functions' documented limit per output value. The service does not enforce it, so this module does:
``hdr``, ``title_q`` and ``series_q`` are Function outputs and never exceed it."""
BODY_MAX_CHARS: Final = 20_000
"""MediaTailor Functions' limit on ``response.body`` (a longer body reads as null)."""
TEXT_MAX_CHARS: Final = 200
"""Free text (title, series, season, rating, producer, keywords) is cut to this many characters."""
_URI_SAFE: Final = "!~*'()"
"""With ``quote``'s own unreserved characters, exactly what JavaScript's ``encodeURIComponent`` leaves alone."""


class Producer(BaseModel):
    """OpenRTB ``content.producer``."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)

    name: str = Field("", max_length=TEXT_MAX_CHARS, description="Producer or studio.")
    domain: str = Field("", description="The producer's web domain.")


class ContentIds(BaseModel):
    """The title's ids in one registry (OpenRTB 2.6 ``content.data[]`` with ``name`` and ``cids``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)

    name: str = Field(description="Registry domain; the first entry is the publisher's own.")
    ids: list[str] = Field(min_length=1, description="The title's ids in that registry.")


class CatalogContext(BaseModel):
    """One title's show-level metadata (``catalog/ctx/v1/<id>.json``). Absent text is ``""``, absent numbers null."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_serialization_defaults_required=True)

    v: Literal[1] = Field(1, description="Schema version.")
    id: str = Field(pattern=CONTENT_ID_PATTERN, max_length=64, description="Catalog content id (OpenRTB content.id).")
    title: str = Field(max_length=TEXT_MAX_CHARS, description="Title.")
    series: str = Field(max_length=TEXT_MAX_CHARS, description="Show of an episode, or franchise of a film.")
    season: str = Field(max_length=TEXT_MAX_CHARS, description="Season label.")
    episode: int | None = Field(ge=0, description="Episode number.")
    type: ContentType | Literal[""] = Field(description="How the title is packaged; empty when the catalog is silent.")
    genre: str = Field(description="Content Taxonomy 3.1 name of the most apt genre.")
    genres: list[str] = Field(max_length=MAX_GENRES, description="Content Taxonomy 3.1 ids, most apt first.")
    gtax: Literal[9] = Field(CT31, description="Taxonomy of genres: 9, IAB Tech Lab Content Taxonomy 3.1.")
    cat: list[str] = Field(max_length=MAX_GENRES, description="Content Taxonomy 3.1 ids of the content category.")
    cattax: Literal[9] = Field(CT31, description="Taxonomy of cat: 9, IAB Tech Lab Content Taxonomy 3.1.")
    contentrating: str = Field(max_length=TEXT_MAX_CHARS, description="Parental rating.")
    len: int | None = Field(ge=0, description="Running time in seconds.")
    language: str = Field(pattern=r"^(?:[a-z]{2})?$", description="ISO 639-1 language; empty when unknown.")
    prodq: int | None = Field(ge=0, le=3, description="AdCOM production quality.")
    producer: Producer | None = Field(description="Producer.")
    kwarray: list[str] = Field(description="Descriptive tags that are not genres.")
    cids: list[ContentIds] = Field(min_length=1, description="Ids by registry, the publisher's first.")
    title_q: str = Field(max_length=OUTPUT_MAX_CHARS, description="title, encoded as by encodeURIComponent.")
    series_q: str = Field(max_length=OUTPUT_MAX_CHARS, description="series, encoded as by encodeURIComponent.")
    hdr: str = Field(max_length=OUTPUT_MAX_CHARS, description="Compact form for the x-fast-ctx request header.")
    updated: AwareDatetime = Field(description="When the publisher wrote the object.")

    @model_validator(mode="after")
    def _fits_a_function_response(self) -> Self:
        if (size := len(self.model_dump_json())) > BODY_MAX_CHARS:
            raise ValueError(f"the context object of {self.id} has {size} characters, more than {BODY_MAX_CHARS}")
        return self


def context_key(content_id: str) -> str:
    """Media-bucket key (and CDN path) of the context object of ``content_id``."""
    return f"{CONTEXT_PREFIX}{content_id}.json"


def build_context(
    item: ContentItem, *, genre_map: GenreMap, publisher_domain: str, updated: datetime
) -> CatalogContext:
    """The context object of ``item``.

    Args:
        item: the catalog title.
        genre_map: catalog genre tags → Content Taxonomy 3.1 (``taxonomy.default_genre_map()`` ships with the code).
        publisher_domain: the publisher's domain, the registry of the catalog's own content ids.
        updated: when the object is written; any time zone, stored as UTC seconds.

    Raises:
        ValueError: ``publisher_domain`` is not a domain, ``updated`` is naive, or the object would exceed the
            Functions response limit (:data:`BODY_MAX_CHARS`).
    """
    if not is_domain(publisher_domain):
        raise ValueError(f"publisher_domain must be a domain such as example.com, got {publisher_domain!r}")
    if updated.tzinfo is None:
        raise ValueError("updated must be timezone-aware")
    resolved = genre_map.resolve(item.genres)
    genres = [genre.id for genre in resolved.genres]
    title, series = _text(item.title), _text(item.series_title)
    rating = _text(item.rating)
    length = None if item.duration_ms is None else (item.duration_ms + 500) // 1000
    return CatalogContext(
        id=item.content_id,
        title=title,
        series=series,
        season=_text(item.season),
        episode=item.episode,
        type=item.content_type or "",
        genre=resolved.genres[0].name if resolved.genres else "",
        genres=genres,
        cat=genres,
        contentrating=rating,
        len=length,
        language=item.language or "",
        prodq=item.prodq,
        producer=(
            Producer(name=_text(item.producer_name), domain=item.producer_domain or "")
            if item.producer_name or item.producer_domain
            else None
        ),
        kwarray=_unique([*resolved.other_tags, *map(_text, item.keywords or ())]),
        cids=_content_ids(item, publisher_domain),
        title_q=_quoted(title, OUTPUT_MAX_CHARS),
        series_q=_quoted(series, OUTPUT_MAX_CHARS),
        hdr=header(
            item.content_id,
            genres=genres,
            cat=genres,
            rating=rating,
            length=length,
            language=item.language,
            prodq=item.prodq,
        ),
        updated=updated.astimezone(UTC).replace(microsecond=0),
    )


def header(
    content_id: str,
    *,
    genres: Iterable[str] = (),
    cat: Iterable[str] = (),
    rating: str = "",
    length: int | None = None,
    language: str | None = None,
    prodq: int | None = None,
) -> str:
    """The ``hdr`` form, ``v1;id=<id>;g=<ids>;c=<ids>;r=<rating>;l=<s>;la=<lang>;pq=<q>``, at most
    :data:`OUTPUT_MAX_CHARS` characters.

    Absent values are left out. Ids are Content Taxonomy 3.1 ids, which are alphanumeric, and content ids are
    URL-safe; the rating is percent-encoded (as by ``encodeURIComponent``), so no value holds ``;``, ``=`` or ``,``.
    Every element but the rating has a bounded length, and the rating is cut, at a character boundary, to what fits.
    """
    head = [f"v{CONTEXT_VERSION}", f"id={content_id}"]
    head += [f"{key}={','.join(ids)}" for key, ids in (("g", list(genres)), ("c", list(cat))) if ids]
    tail = [f"{key}={value}" for key, value in (("l", length), ("la", language), ("pq", prodq)) if value is not None]
    budget = OUTPUT_MAX_CHARS - len(";".join([*head, *tail])) - len(";r=")
    middle = [f"r={encoded}"] if (encoded := _quoted(rating, budget)) else []
    return ";".join([*head, *middle, *tail])


def _content_ids(item: ContentItem, publisher_domain: str) -> list[ContentIds]:
    """The publisher's id first, then each registry's in domain order; one entry per registry."""
    registries: dict[str, list[str]] = {publisher_domain: [item.content_id]}
    for domain, id_ in sorted((item.external_ids or {}).items()):
        ids = registries.setdefault(domain, [])
        if id_ not in ids:
            ids.append(id_)
    return [ContentIds(name=name, ids=ids) for name, ids in registries.items()]


def _text(value: str | None) -> str:
    """``value`` with runs of whitespace (newlines included) collapsed to one space, cut to TEXT_MAX_CHARS."""
    return " ".join((value or "").split())[:TEXT_MAX_CHARS].rstrip()


def _quoted(text: str, limit: int) -> str:
    """``encodeURIComponent(text)``, cut at a character boundary to at most ``limit`` characters."""
    out: list[str] = []
    size = 0
    for char in text:
        piece = quote(char, safe=_URI_SAFE)
        if size + len(piece) > limit:
            break
        out.append(piece)
        size += len(piece)
    return "".join(out)


def _unique(values: Iterable[str]) -> list[str]:
    """Non-empty values in first-seen order, without repeats."""
    return list(dict.fromkeys(value for value in values if value))
