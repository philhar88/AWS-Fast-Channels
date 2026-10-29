"""The Content Taxonomy 3.1 id list and the catalog genre map (IAB CTV Genre Mapping)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from fast_channels.core.crawl.taxonomy import (
    CT31,
    MAX_GENRES,
    Genre,
    default_genre_map,
    load_genre_map,
    load_taxonomy,
)

TAXONOMY = load_taxonomy()


def test_the_id_list_holds_the_31_categories_and_the_descriptive_vectors() -> None:
    assert CT31 == 9
    assert len(TAXONOMY) == 704 + 47
    assert TAXONOMY["652"] == "Science Fiction"
    assert TAXONOMY["KHPC5A"] == "True Crime"  # ids are strings, some alphanumeric
    assert TAXONOMY["SPSHQ5"] == "Genres"
    assert TAXONOMY["1020"] == "News"  # a descriptive vector (Content Purpose > Informational > News)
    assert TAXONOMY["20L2OV"] == "Movie"  # Content Form Factor > Video > Movie


def test_the_shipped_map_uses_only_known_ids_and_covers_every_ctv_genre() -> None:
    genre_map = default_genre_map()
    assert genre_map is default_genre_map()  # loaded once
    assert len(genre_map.tags) == 44  # one tag per CTV genre of the IAB mapping
    assert all(genre.id in TAXONOMY and genre.name == TAXONOMY[genre.id] for genre in genre_map.tags.values())
    assert all(tag == tag.lower() for tag in genre_map.tags)


@pytest.mark.parametrize(
    ("tag", "genre"),
    [
        ("action", Genre("325", "Action/Adventure")),
        ("adventure", Genre("325", "Action/Adventure")),
        ("animation", Genre("641", "Animation & Anime")),
        ("comedy", Genre("646", "Comedy")),
        ("sitcom", Genre("646", "Comedy")),
        ("documentary", Genre("332", "Documentary")),
        ("fantasy", Genre("335", "Fantasy")),
        ("sci-fi", Genre("652", "Science Fiction")),
        ("crime", Genre("KHPC5A", "True Crime")),
        ("paranormal", Genre("244", "Paranormal Phenomena")),
        ("news", Genre("1020", "News")),
    ],
)
def test_ctv_genre_mapping(tag: str, genre: Genre) -> None:
    assert default_genre_map().tags[tag] == genre


def test_resolve_keeps_catalog_order_drops_repeated_ids_and_keeps_three() -> None:
    resolved = default_genre_map().resolve(
        ["Animation", " fantasy ", "adventure", "action", "surreal", "", "drama", "comedy", "surreal", "Trailer"]
    )
    assert [genre.id for genre in resolved.genres] == ["641", "335", "325"]  # action repeats 325; drama is a fourth
    assert len(resolved.genres) == MAX_GENRES
    assert resolved.other_tags == ("surreal", "trailer")


def test_resolve_with_no_genres() -> None:
    resolved = default_genre_map().resolve([])
    assert resolved.genres == () and resolved.other_tags == ()


def _map(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "genre-map.toml"
    path.write_text(text)
    return path


def test_unknown_ids_are_rejected(tmp_path: Path) -> None:
    path = _map(tmp_path, '[ctv]\n"Made Up" = "999999"\n"Drama" = "647"\n[tags]\ndrama = "Drama"\n')
    with pytest.raises(ValueError, match=r"'999999', which is not a Content Taxonomy 3\.1 id"):
        load_genre_map(path)


def test_tags_must_be_lower_case_and_name_a_ctv_genre(tmp_path: Path) -> None:
    path = _map(tmp_path, '[ctv]\n"Drama" = "647"\n[tags]\nDrama = "Drama"\nnoir = "Film Noir"\n')
    with pytest.raises(ValueError, match="lower case") as error:
        load_genre_map(path)
    assert "'Film Noir', which is not a CTV genre of the map" in str(error.value)


def test_a_custom_map_and_taxonomy(tmp_path: Path) -> None:
    path = _map(tmp_path, '[ctv]\n"Drama" = "X1"\n[tags]\ndrama = "Drama"\nmelodrama = "Drama"\n')
    genre_map = load_genre_map(path, taxonomy={"X1": "Drama"})
    assert genre_map.resolve(["melodrama", "drama"]).genres == (Genre("X1", "Drama"),)


def test_the_map_file_has_exactly_two_tables(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        load_genre_map(_map(tmp_path, '[ctv]\n[tags]\n[aliases]\nx = "y"\n'))


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("1\tOne\n1\tAgain\n", "id 1 is listed twice"),
        ("1 One\n", "expected '<id><TAB><name>'"),
        ("1\t\n", "expected '<id><TAB><name>'"),
        ("1\tOne\tExtra\n", "expected '<id><TAB><name>'"),
    ],
)
def test_malformed_id_lists_are_rejected(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "ids.txt"
    path.write_text(f"# header\n\n{text}")
    with pytest.raises(ValueError, match=error):
        load_taxonomy(path)
