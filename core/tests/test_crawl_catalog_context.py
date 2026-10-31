"""The catalog context object: golden objects for the demo titles, the limits, and the header form."""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.catalog import ContentItem, ContentType
from fast_channels.core.crawl.catalog_context import (
    BODY_MAX_CHARS,
    CACHE_CONTROL,
    CONTEXT_PREFIX,
    OUTPUT_MAX_CHARS,
    TEXT_MAX_CHARS,
    CatalogContext,
    build_context,
    context_key,
    digest,
    header,
)
from fast_channels.core.crawl.taxonomy import default_genre_map

REPO = Path(__file__).resolve().parents[2]
PUBLISHER = "example.com"
UPDATED = datetime(2026, 10, 28, tzinfo=UTC)
SAMPLES = {
    title["content_id"]: ContentItem.model_validate(title)
    for title in json.loads((REPO / "assets/sample-catalog.json").read_text())["titles"]
}


def build(item: ContentItem, **overrides: Any) -> CatalogContext:
    arguments: dict[str, Any] = {"genre_map": default_genre_map(), "publisher_domain": PUBLISHER, "updated": UPDATED}
    return build_context(item, **(arguments | overrides))


def dumped(context: CatalogContext) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(context.model_dump_json())
    return document


def test_tears_of_steel_is_the_designed_object() -> None:
    expected = {
        "v": 1,
        "id": "tears-of-steel",
        "title": "Tears of Steel",
        "series": "",
        "season": "",
        "episode": None,
        "type": "movie",
        "genre": "Science Fiction",
        "genres": ["652", "325"],
        "gtax": 9,
        "cat": ["652", "325"],
        "cattax": 9,
        "contentrating": "TV-14",
        "len": 734,
        "language": "en",
        "prodq": 1,
        "producer": {"name": "Blender Foundation", "domain": "blender.org"},
        "kwarray": ["live-action"],
        "cids": [{"name": PUBLISHER, "ids": ["tears-of-steel"]}],
        "title_q": "Tears%20of%20Steel",
        "series_q": "",
        "hdr": "v1;id=tears-of-steel;g=652,325;c=652,325;r=TV-14;l=734;la=en;pq=1",
        "updated": "2026-10-28T00:00:00Z",
    }
    document = dumped(build(SAMPLES["tears-of-steel"]))
    assert document == expected
    assert list(document) == list(expected)  # the published key order


@pytest.mark.parametrize(
    ("content_id", "genres", "genre", "kwarray", "rating", "length"),
    [
        ("sintel", ["641", "335", "325"], "Animation & Anime", [], "TV-14", 888),
        ("tears-of-steel", ["652", "325"], "Science Fiction", ["live-action"], "TV-14", 734),
        ("elephants-dream", ["641"], "Animation & Anime", ["surreal"], "TV-PG", 654),
        ("sintel-trailer", ["641", "335"], "Animation & Anime", ["trailer"], "TV-PG", 52),
    ],
)
def test_the_demo_titles(
    content_id: str, genres: list[str], genre: str, kwarray: list[str], rating: str, length: int
) -> None:
    context = build(SAMPLES[content_id])
    assert (context.genres, context.cat, context.genre, context.kwarray) == (genres, genres, genre, kwarray)
    assert (context.contentrating, context.len, context.language, context.prodq) == (rating, length, "en", 1)
    assert (context.series, context.season, context.episode) == ("", "", None)  # no episodic demo titles
    assert context.hdr == (
        f"v1;id={content_id};g={','.join(genres)};c={','.join(genres)};r={rating};l={length};la=en;pq=1"
    )


def test_an_episode_with_external_ids() -> None:
    item = ContentItem(
        content_id="ep-301",
        title="The Lighthouse",
        series_title="Coastal Stories: Season of Storms",
        season="Season 3",
        episode=4,
        content_type=ContentType.EPISODE,
        rating="TV-PG",
        genres=["documentary", "history"],
        keywords=["coastal", " Coastal ", "", "lighthouses"],
        duration_ms=1_320_499,
        external_ids={"metadata.example.org": "EP000123", PUBLISHER: "ep-301-v2"},
    )
    context = build(item)
    assert (context.series, context.season, context.episode, context.type) == (
        "Coastal Stories: Season of Storms",
        "Season 3",
        4,
        "episode",
    )
    assert context.series_q == "Coastal%20Stories%3A%20Season%20of%20Storms"
    assert (context.genres, context.genre) == (["332", "EZWB7V"], "Documentary")
    assert context.kwarray == ["coastal", "Coastal", "lighthouses"]
    assert context.len == 1320  # rounded to the nearest second
    assert [entry.model_dump() for entry in context.cids] == [
        {"name": PUBLISHER, "ids": ["ep-301", "ep-301-v2"]},  # the publisher's registry comes first, merged
        {"name": "metadata.example.org", "ids": ["EP000123"]},
    ]
    assert context.producer is None and context.prodq is None and context.language == ""
    assert context.hdr == "v1;id=ep-301;g=332,EZWB7V;c=332,EZWB7V;r=TV-PG;l=1320"


def test_a_bare_title_leaves_absent_values_out() -> None:
    context = build(ContentItem(content_id="bare", title="Bare"), updated=datetime(2026, 1, 2, 3, 4, 5, 678, UTC))
    assert context.hdr == "v1;id=bare"
    assert (context.genre, context.genres, context.cat, context.kwarray) == ("", [], [], [])
    assert (context.gtax, context.cattax) == (9, 9)  # sent explicitly even when empty
    assert (context.type, context.contentrating, context.len, context.producer) == ("", "", None, None)
    assert dumped(context)["updated"] == "2026-01-02T03:04:05Z"


def test_without_a_publisher_domain_the_id_is_only_in_id() -> None:
    item = ContentItem(content_id="x", title="X", external_ids={"metadata.example.org": "M1"})
    assert [entry.name for entry in build(item, publisher_domain=None).cids] == ["metadata.example.org"]
    assert build(ContentItem(content_id="x", title="X"), publisher_domain=None).cids == []


def test_the_digest_ignores_only_the_write_time() -> None:
    first = build(SAMPLES["sintel"])
    assert digest(first) == digest(build(SAMPLES["sintel"], updated=UPDATED + timedelta(days=1)))
    changed = SAMPLES["sintel"].model_copy(update={"keywords": ["dragon"]})
    assert digest(first) != digest(build(changed))
    assert len(digest(first)) == 64


def test_a_producer_domain_alone_is_a_producer() -> None:
    context = build(ContentItem(content_id="x", title="X", producer_domain="studio.example"))
    assert context.producer is not None and context.producer.model_dump() == {"name": "", "domain": "studio.example"}


def test_updated_is_stored_in_utc() -> None:
    context = build(SAMPLES["sintel"], updated=datetime(2026, 10, 28, 2, tzinfo=timezone(timedelta(hours=2))))
    assert dumped(context)["updated"] == "2026-10-28T00:00:00Z"


@pytest.mark.parametrize(
    ("text", "encoded"),
    [
        ("Tears of Steel", "Tears%20of%20Steel"),
        ("Sintel (trailer)", "Sintel%20(trailer)"),
        ("a;b=c,d/é!*'()~_.-", "a%3Bb%3Dc%2Cd%2F%C3%A9!*'()~_.-"),
        ('Ep "1" \\ & ?#', "Ep%20%221%22%20%5C%20%26%20%3F%23"),
        ("😀", "%F0%9F%98%80"),
    ],
)
def test_q_keys_are_encoded_as_by_encode_uri_component(text: str, encoded: str) -> None:
    assert build(ContentItem(content_id="x", title=text)).title_q == encoded


def test_free_text_is_one_line_and_at_most_200_characters() -> None:
    context = build(ContentItem(content_id="x", title=f"  A\nvery\t\tlong {'é' * 400}", rating="TV-14\n"))
    assert context.title.startswith("A very long ") and len(context.title) == TEXT_MAX_CHARS
    assert context.contentrating == "TV-14"
    assert len(context.title_q) <= OUTPUT_MAX_CHARS  # 188 é are 1,128 encoded characters: cut at a boundary
    assert context.title_q.endswith("%C3%A9") and (len(context.title_q) - len("A%20very%20long%20")) % 6 == 0


def test_the_header_always_fits_the_output_limit() -> None:
    rating = "R;=,é" * 40  # 200 characters, 760 encoded
    item = ContentItem(
        content_id="x" * 64, title="t", rating=rating, genres=["action", "drama", "comedy"], duration_ms=10**12
    )
    context = build(item, publisher_domain="a.example")
    assert "r=R%3B%3D%2C%C3%A9R" in context.hdr and len(context.hdr) <= OUTPUT_MAX_CHARS
    long = header("x" * 64, genres=["KHPC5A"] * 3, cat=["KHPC5A"] * 3, rating="😀" * 500, length=10**9, prodq=1)
    assert len(long) <= OUTPUT_MAX_CHARS and len(long) > OUTPUT_MAX_CHARS - len("%F0%9F%98%80")
    assert long.endswith("%F0%9F%98%80;l=1000000000;pq=1")  # never a split escape
    assert header("x", rating="") == "v1;id=x"


def test_an_object_beyond_the_response_body_limit_is_refused() -> None:
    keywords = [f"keyword-{n:05}-{'k' * 180}" for n in range(120)]
    with pytest.raises(ValidationError, match=f"more than {BODY_MAX_CHARS}"):
        build(ContentItem(content_id="big", title="Big", keywords=keywords))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"publisher_domain": "localhost"}, "publisher_domain must be a domain"),
        ({"updated": datetime(2026, 10, 28)}, "timezone-aware"),
    ],
)
def test_build_refuses_bad_arguments(overrides: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        build(SAMPLES["sintel"], **overrides)


def test_the_model_enforces_the_contract() -> None:
    document = dumped(build(SAMPLES["sintel"]))
    assert CatalogContext.model_validate_json(json.dumps(document)) == build(SAMPLES["sintel"])
    for change in (
        {"v": 2},
        {"gtax": 1},
        {"cattax": 1},
        {"genres": ["1", "2", "3", "4"]},
        {"hdr": "x" * (OUTPUT_MAX_CHARS + 1)},
        {"title_q": "x" * (OUTPUT_MAX_CHARS + 1)},
        {"cids": [{"name": "example.org", "ids": []}]},
        {"id": "-bad"},
        {"extra": "field"},
        {"updated": "2026-10-28T00:00:00"},
        {"type": "series"},
        {"language": "EN"},
    ):
        with pytest.raises(ValidationError):
            CatalogContext.model_validate(document | change)


def test_object_keys() -> None:
    assert CONTEXT_PREFIX == "catalog/ctx/v1/" and CACHE_CONTROL == "max-age=300"
    assert context_key("tears-of-steel") == "catalog/ctx/v1/tears-of-steel.json"
