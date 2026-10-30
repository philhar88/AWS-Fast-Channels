"""OpenRTB ``app.content`` from the catalog context object and from the header: the D§9.6 mapping by mode and
Prebid Server release, and the header grammar."""

import json
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CatalogContext, build_context, header
from fast_channels.core.crawl.ortb_content import (
    NAMING,
    Channel,
    Metadata,
    app_content,
    channel_of,
    content_id,
    from_context,
    from_header,
)
from fast_channels.core.crawl.taxonomy import default_genre_map, load_taxonomy
from fast_channels.core.pds.models import Mode

REPO = Path(__file__).resolve().parents[2]
PUBLISHER = "example.com"
CHANNEL = Channel("fastm-demo", PUBLISHER)
NAMES = load_taxonomy()
SAMPLES = {
    title["content_id"]: ContentItem.model_validate(title)
    for title in json.loads((REPO / "assets/sample-catalog.json").read_text())["titles"]
}


def context(content: str = "tears-of-steel", **fields: Any) -> CatalogContext:
    item = SAMPLES[content].model_copy(update=fields)
    return build_context(
        item, genre_map=default_genre_map(), publisher_domain=PUBLISHER, updated=datetime(2026, 10, 28, tzinfo=UTC)
    )


TEARS_OF_STEEL = {
    "id": "tears-of-steel",
    "title": "Tears of Steel",
    "genre": "Science Fiction",
    "contentrating": "TV-14",
    "len": 734,
    "language": "en",
    "prodq": 1,
    "producer": {"name": "Blender Foundation", "domain": "blender.org"},
    "kwarray": ["live-action"],
    "channel": {"id": "fastm-demo", "domain": PUBLISHER},
    "cat": ["652", "325"],
    "cattax": 9,
}
"""The OpenRTB 2.5 fields of Tears of Steel, which both Prebid Server releases model."""
NEWER = {
    "realtime": 0,
    "firstbroadcast": 0,
    "genres": ["652", "325"],
    "gtax": 9,
    "data": [{"name": PUBLISHER, "cids": ["tears-of-steel"]}],
}
"""Its OpenRTB 2.6-202505 fields."""


def test_every_field_is_native() -> None:
    """Prebid Server Java 4.4.0 models the OpenRTB 2.6-202505 content fields: none travels in ``ext``."""
    content = app_content(from_context(context()), mode=Mode.CONTEXTUAL, channel=CHANNEL)
    assert content == TEARS_OF_STEEL | NEWER


@pytest.mark.parametrize("mode", [Mode.FULL, Mode.KIDS])
def test_full_and_kids_send_what_contextual_sends(mode: Mode) -> None:
    metadata = from_context(context())
    contextual = app_content(metadata, mode=Mode.CONTEXTUAL, channel=CHANNEL)
    assert app_content(metadata, mode=mode, channel=CHANNEL) == contextual


def test_pseudonymous_sends_genre_and_category_never_the_title() -> None:
    content = app_content(from_context(context()), mode=Mode.PSEUDONYMOUS, channel=CHANNEL)
    newer = {"realtime": 0, "firstbroadcast": 0, "genres": ["652", "325"], "gtax": 9}
    common = {
        "genre": "Science Fiction",
        "cat": ["652", "325"],
        "cattax": 9,
        "contentrating": "TV-14",
        "language": "en",
        "prodq": 1,
        "channel": {"id": "fastm-demo", "domain": PUBLISHER},
    }
    assert content == common | newer
    assert "Tears" not in json.dumps(content) and "tears-of-steel" not in json.dumps(content)


def test_safe_sends_nothing() -> None:
    assert app_content(from_context(context()), mode=Mode.SAFE, channel=CHANNEL) == {}


def test_an_episode_carries_its_series_season_and_number() -> None:
    episode = context("sintel", series_title="Open Movies", season="1", episode=0)
    content = app_content(from_context(episode), mode=Mode.FULL)
    assert (content["series"], content["season"], content["episode"]) == ("Open Movies", "1", 0)
    assert "channel" not in content, "no channel without a sibling configuration"
    pseudonymous = app_content(from_context(episode), mode=Mode.PSEUDONYMOUS)
    assert not NAMING & pseudonymous.keys()


@pytest.mark.parametrize(("content", "mode"), list(product(SAMPLES, Mode)))
def test_taxonomies_are_explicit_and_every_value_is_present(content: str, mode: Mode) -> None:
    """Validator V7: ``cattax`` and ``gtax`` 9 wherever ``cat`` and ``genres`` are sent; and no empty values."""
    sent = app_content(from_context(context(content)), mode=mode, channel=CHANNEL)
    assert ("cat" in sent) == ("cattax" in sent) and sent.get("cattax", 9) == 9
    assert ("genres" in sent) == ("gtax" in sent) and sent.get("gtax", 9) == 9
    assert all(value not in (None, "", []) for value in sent.values())
    if mode is Mode.SAFE:
        assert sent == {}


@pytest.mark.parametrize(
    ("upid", "expected"),
    [
        ("FST3tears-of-steel", "tears-of-steel"),
        ("tears-of-steel", "tears-of-steel"),
        ("FST3", None),
        ("", None),
        ("FST3bad id", None),
        ("-leading-dash", None),
        ("a" * 65, None),
        ("a" * 64, "a" * 64),
    ],
)
def test_the_content_id_of_a_upid(upid: str, expected: str | None) -> None:
    assert content_id(upid) == expected


@pytest.mark.parametrize(("pc", "channel"), [("fastm-demo-p-b", "fastm-demo"), ("fastm-demo-p-k", "fastm-demo")])
def test_the_channel_of_a_sibling(pc: str, channel: str) -> None:
    assert channel_of(pc, domain=PUBLISHER) == Channel(channel, PUBLISHER)


@pytest.mark.parametrize("pc", ["fastm-demo", "fastm-demo-p-", "fastm-demo-p-ab"])
def test_no_channel_for_other_configurations(pc: str) -> None:
    assert channel_of(pc) is None


@pytest.mark.parametrize("content", list(SAMPLES))
def test_the_header_carries_the_context_objects_fields_but_the_title(content: str) -> None:
    ctx = context(content)
    parsed = from_header(ctx.hdr, genre_names=NAMES)
    assert parsed == Metadata(
        id=ctx.id,
        genre=ctx.genre,
        genres=tuple(ctx.genres),
        cat=tuple(ctx.cat),
        contentrating=ctx.contentrating,
        len=ctx.len,
        language=ctx.language,
        prodq=ctx.prodq,
    )


def test_header_content_is_the_snapshots_without_what_names_the_title() -> None:
    ctx = context()
    parsed = from_header(ctx.hdr, genre_names=NAMES)
    assert parsed is not None
    full = app_content(from_context(ctx), mode=Mode.CONTEXTUAL, channel=CHANNEL)
    from_hdr = app_content(parsed, mode=Mode.CONTEXTUAL, channel=CHANNEL)
    assert from_hdr == {k: v for k, v in full.items() if k not in {"title", "producer", "kwarray", "data"}}


def test_a_percent_encoded_rating_is_decoded() -> None:
    value = header("x1", rating="TV-14; L,V")
    parsed = from_header(value, genre_names=NAMES)
    assert parsed is not None and parsed.contentrating == "TV-14; L,V"


def test_a_minimal_header() -> None:
    assert from_header("v1;id=x1", genre_names=NAMES) == Metadata(id="x1")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "v2;id=x1",
        "id=x1",
        "v1",
        "v1;id=",
        "v1;id=bad id",
        "v1;id=x1;id=x2",
        "v1;id=x1;zz=1",
        "v1;id=x1;g",
        "v1;id=x1;g=641,,335",
        "v1;id=x1;g=1,2,3,4",
        "v1;id=x1;c=6 41",
        "v1;id=x1;la=EN",
        "v1;id=x1;la=eng",
        "v1;id=x1;l=-1",
        "v1;id=x1;l=12.5",
        "v1;id=x1;pq=4",
        "v1;id=x1;r=%FF",
    ],
)
def test_a_header_outside_the_grammar_is_ignored(value: str) -> None:
    assert from_header(value, genre_names=NAMES) is None
