from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from fast_channels.core import profiles
from fast_channels.core.profiles import CATALOGUE, DEFAULT_PACKAGE_PROFILES, applicable, profile

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

CAPTURE = Path(__file__).parent / "data/hls-ts"


def test_one_hls_profile() -> None:
    assert [p.name for p in CATALOGUE] == ["hls"]
    assert DEFAULT_PACKAGE_PROFILES == ("hls",)
    assert all(profile(name).name == name for name in DEFAULT_PACKAGE_PROFILES)
    for removed in ("hls-avc", "hls-hevc", "hls-uhd", "dash-avc"):  # no codec subsets and no DASH
        with pytest.raises(ValueError, match=r"unknown rendition profile .*; known: \['hls'\]"):
            profile(removed)
    assert not hasattr(profiles, "ManifestFormat")


def test_manifest_key_is_the_group_manifest() -> None:
    assert profile("hls").manifest_key("assets/a/lv/") == "assets/a/lv/main.m3u8"
    assert profile("hls").manifest_key("assets/a/lv") == "assets/a/lv/main.m3u8"
    # A real encode writes exactly that multivariant next to its media playlists.
    for asset in ("content", "slate"):
        assert (CAPTURE / profile("hls").manifest_key(asset)).is_file()


def test_every_ladder_writes_the_hls_profile(standard: Ladder) -> None:
    assert applicable(standard) == [profile("hls")]


def test_profiles_are_immutable() -> None:
    with pytest.raises(ValidationError):
        profile("hls").name = "dash"  # type: ignore[misc]
