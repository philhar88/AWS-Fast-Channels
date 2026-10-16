from typing import TYPE_CHECKING

import pytest

from fast_channels.core.profiles import (
    CATALOGUE,
    DEFAULT_PACKAGE_PROFILES,
    ManifestFormat,
    applicable,
    profile,
    selected_outputs,
)

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder


def test_catalogue() -> None:
    assert [p.name for p in CATALOGUE] == [
        "hls",
        "hls-avc",
        "hls-hevc",
        "hls-av1",
        "hls-sd",
        "hls-hd",
        "hls-uhd",
        "dash-avc",
        "dash-hevc",
        "dash-av1",
    ]
    assert DEFAULT_PACKAGE_PROFILES == ("hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc")
    assert all(profile(name).name == name for name in DEFAULT_PACKAGE_PROFILES)
    with pytest.raises(ValueError, match="unknown rendition profile"):
        profile("dash")  # the combined MPD mixes codecs in one AdaptationSet and is never exposed


def test_manifest_keys() -> None:
    assert profile("hls").manifest_key("assets/a/lv/") == "assets/a/lv/main.m3u8"
    assert profile("hls-avc").manifest_key("assets/a/lv") == "assets/a/lv/main_avc.m3u8"
    assert profile("dash-avc").manifest_key("assets/a/lv") == "assets/a/lv/main_avc.mpd"
    assert profile("hls-uhd").manifest_suffix == "_uhd"
    assert profile("dash-av1").format is ManifestFormat.DASH


def test_profiles_sharing_a_manifest_select_the_same_renditions(uhd: Ladder) -> None:
    for a in CATALOGUE:
        for b in CATALOGUE:
            if a.manifest_suffix == b.manifest_suffix:
                assert selected_outputs(uhd, a) == selected_outputs(uhd, b)


def test_applicable_and_selection(standard: Ladder, uhd: Ladder) -> None:
    assert "hls-uhd" not in [p.name for p in applicable(standard)]
    assert [p.name for p in applicable(uhd)] == [p.name for p in CATALOGUE]
    assert selected_outputs(standard, profile("hls-hevc")) == [
        "_hevc-1080p",
        "_hevc-720p",
        "_hevc-540p",
        "_hevc-360p",
        "_aac-lc-128",
        "_aac-he1-64",
    ]
    sd = selected_outputs(standard, profile("hls-sd"))
    assert "_avc-234p" in sd and "_avc-720p-a" not in sd and "_aac-he2-32" in sd
