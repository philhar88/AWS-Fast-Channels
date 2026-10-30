"""OpenRTB 2.6 ``app.content`` of a break from its title's metadata (DESIGN §9.6), for the PDS's ``/policy`` patch.

The metadata comes from the title's catalog context object (:func:`from_context`) or, while the PDS's copy of the
catalog lacks the title, from the ``x-fast-ctx`` header of the PRE_ADS lookup (:func:`from_header`), which carries the
ids, genres, categories, rating, length, language and quality but never the title.

What a break sends depends on its privacy mode (:func:`app_content`):

- ``FULL``, ``CONTEXTUAL`` and ``KIDS`` (whose patch sets ``regs.coppa``) get every field;
- ``PSEUDONYMOUS`` gets the genres and categories, the rating, language and quality and the channel, but nothing that
  names the title (:data:`NAMING`): what is watched, paired with a device, touches video-privacy law;
- ``SAFE`` gets nothing: no third party sees the request.

The stored request already sets ``livestream``, ``context`` and ``cattax``, so the patch adds only the title's
fields. Prebid Server Java 3.43.0 silently drops the OpenRTB 2.6-202505 content fields it does not model (``genres``,
``gtax``, ``realtime``, ``firstbroadcast``, ``data[].cids``); before 4.4.0 they travel in ``app.content.ext``, and from
4.4.0 natively (:func:`native_2_6`). Under the patch's first-party-data rule (``fpd.bidders``: the own book only),
Prebid Server passes ``app.content.data`` to the own book alone, so third parties see the content id in ``id``.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote

from fast_channels.core.catalog import CONTENT_ID_PATTERN
from fast_channels.core.crawl.catalog_context import CONTEXT_VERSION, TEXT_MAX_CHARS, CatalogContext
from fast_channels.core.crawl.taxonomy import CT31, MAX_GENRES
from fast_channels.core.pds.models import Mode
from fast_channels.core.timeline import UPID_FORMAT_ID

if TYPE_CHECKING:
    from pydantic import JsonValue

NATIVE_FROM: Final = (4, 4, 0)
"""The first Prebid Server Java release that models every OpenRTB 2.6 content field the patch sends."""
NOT_LIVE: Final = 0
"""``realtime`` and ``firstbroadcast`` of VOD-sourced FAST: not a live event, not a first broadcast."""
NAMING: Final = frozenset({"id", "title", "series", "season", "episode", "len", "kwarray", "producer", "data"})
"""Fields that name or fingerprint the title, which PSEUDONYMOUS breaks never send."""
CONTENT_ID_MAX: Final = 64
_HEADER_KEYS: Final = frozenset({"id", "g", "c", "r", "l", "la", "pq"})
_TAXONOMY_IDS: Final = re.compile(rf"[A-Za-z0-9]{{1,16}}(?:,[A-Za-z0-9]{{1,16}}){{0,{MAX_GENRES - 1}}}")
_LANGUAGE: Final = re.compile(r"[a-z]{2}")
_COUNT: Final = re.compile(r"[0-9]{1,9}")
_SIBLING: Final = re.compile(r"(?P<channel>.+)-p-[a-z0-9]")


@dataclass(frozen=True, slots=True)
class Channel:
    """OpenRTB ``content.channel``: the FAST channel the break airs on."""

    id: str
    domain: str | None = None

    def ortb(self) -> dict[str, JsonValue]:
        return {"id": self.id, **({"domain": self.domain} if self.domain else {})}


def channel_of(pc: str, *, domain: str | None = None) -> Channel | None:
    """The channel of a monetized sibling configuration (``<channel>-p-<letter>``), None for any other name."""
    match = _SIBLING.fullmatch(pc)
    return Channel(match["channel"], domain) if match else None


@dataclass(frozen=True, slots=True)
class Metadata:
    """What a break's ``app.content`` is built from. Absent text is ``""``, absent numbers None."""

    id: str
    title: str = ""
    series: str = ""
    season: str = ""
    episode: int | None = None
    genre: str = ""
    """Content Taxonomy 3.1 name of the most apt genre."""
    genres: tuple[str, ...] = ()
    cat: tuple[str, ...] = ()
    contentrating: str = ""
    len: int | None = None
    language: str = ""
    prodq: int | None = None
    producer: Mapping[str, str] | None = None
    kwarray: tuple[str, ...] = ()
    cids: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """``(registry, ids)`` pairs, the publisher's first."""


def native_2_6(pbs_version: str) -> bool:
    """Whether Prebid Server Java ``pbs_version`` (``3.43.0``, ``4.4.0``) models the 2.6 content fields natively.

    Raises:
        ValueError: ``pbs_version`` is not ``<major>.<minor>.<patch>``.
    """
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pbs_version):
        raise ValueError(f"not a Prebid Server release: {pbs_version!r}")
    major, minor, patch = (int(part) for part in pbs_version.split("."))
    return (major, minor, patch) >= NATIVE_FROM


def content_id(upid: str) -> str | None:
    """The catalog content id of a break's SCTE-35 UPID: ``FST3`` and the id, or the id alone (MediaTailor strips
    the format identifier); None when what is left is not a content id."""
    candidate = upid.removeprefix(UPID_FORMAT_ID)
    return candidate if is_content_id(candidate) else None


def is_content_id(value: str) -> bool:
    """Whether ``value`` is a catalog content id (``ContentItem.content_id``)."""
    return len(value) <= CONTENT_ID_MAX and re.fullmatch(CONTENT_ID_PATTERN, value) is not None


def from_context(context: CatalogContext) -> Metadata:
    """The metadata of a catalog context object."""
    producer = context.producer.model_dump() if context.producer is not None else {}
    return Metadata(
        id=context.id,
        title=context.title,
        series=context.series,
        season=context.season,
        episode=context.episode,
        genre=context.genre,
        genres=tuple(context.genres),
        cat=tuple(context.cat),
        contentrating=context.contentrating,
        len=context.len,
        language=context.language,
        prodq=context.prodq,
        producer={key: value for key, value in producer.items() if value} or None,
        kwarray=tuple(context.kwarray),
        cids=tuple((entry.name, tuple(entry.ids)) for entry in context.cids),
    )


def from_header(value: str, *, genre_names: Mapping[str, str]) -> Metadata | None:
    """The metadata an ``x-fast-ctx`` header carries (``catalog_context.header``), or None when it is empty or not a
    well-formed ``v1`` value. ``genre_names`` maps Content Taxonomy 3.1 ids to names (``taxonomy.load_taxonomy``),
    for ``genre``."""
    version, *parts = value.split(";")
    if version != f"v{CONTEXT_VERSION}":
        return None
    fields: dict[str, str] = {}
    for part in parts:
        key, sep, raw = part.partition("=")
        if not sep or key not in _HEADER_KEYS or key in fields:
            return None
        fields[key] = raw
    identifier, genres, cat = fields.get("id", ""), fields.get("g", ""), fields.get("c", "")
    language, length, prodq = fields.get("la", ""), fields.get("l"), fields.get("pq")
    if (
        not is_content_id(identifier)
        or any(ids and not _TAXONOMY_IDS.fullmatch(ids) for ids in (genres, cat))
        or (language and not _LANGUAGE.fullmatch(language))
        or any(number is not None and not _COUNT.fullmatch(number) for number in (length, prodq))
        or (prodq is not None and int(prodq) > 3)
    ):
        return None
    try:
        rating = unquote(fields.get("r", ""), errors="strict")
    except UnicodeDecodeError:
        return None
    genre_ids = tuple(genres.split(",")) if genres else ()
    return Metadata(
        id=identifier,
        genre=genre_names.get(genre_ids[0], "") if genre_ids else "",
        genres=genre_ids,
        cat=tuple(cat.split(",")) if cat else (),
        contentrating=rating[:TEXT_MAX_CHARS],
        len=int(length) if length is not None else None,
        language=language,
        prodq=int(prodq) if prodq is not None else None,
    )


def app_content(
    metadata: Metadata, *, mode: Mode, native: bool, channel: Channel | None = None
) -> dict[str, JsonValue]:
    """``app.content`` of a break of ``metadata``'s title in ``mode`` (empty in SAFE).

    Args:
        metadata: the title's metadata.
        mode: the break's privacy mode.
        native: send the OpenRTB 2.6-202505 fields natively (Prebid Server Java 4.4.0 and later) rather than in
            ``ext``.
        channel: the channel the break airs on.
    """
    if mode is Mode.SAFE:
        return {}
    content: dict[str, JsonValue] = {
        "id": metadata.id,
        "title": metadata.title,
        "series": metadata.series,
        "season": metadata.season,
        "episode": metadata.episode,
        "genre": metadata.genre,
        "contentrating": metadata.contentrating,
        "len": metadata.len,
        "language": metadata.language,
        "prodq": metadata.prodq,
        "producer": dict(metadata.producer) if metadata.producer else None,
        "kwarray": list(metadata.kwarray) or None,
        "channel": channel.ortb() if channel else None,
    }
    if metadata.cat:
        content |= {"cat": list(metadata.cat), "cattax": CT31}
    newer: dict[str, JsonValue] = {"realtime": NOT_LIVE, "firstbroadcast": NOT_LIVE}
    if metadata.genres:
        newer |= {"genres": list(metadata.genres), "gtax": CT31}
    if metadata.cids:
        newer["data"] = [{"name": name, "cids": list(ids)} for name, ids in metadata.cids]
    if mode is Mode.PSEUDONYMOUS:
        content = {key: value for key, value in content.items() if key not in NAMING}
        newer = {key: value for key, value in newer.items() if key not in NAMING}
    content = {key: value for key, value in content.items() if value not in (None, "")}
    if native:
        return content | newer
    return content | {"ext": newer}
