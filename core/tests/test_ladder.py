import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.ladder import (
    AAC_LC_KBPS,
    BUILTIN_DIR,
    LADDERS_DIR_ENV,
    Audio,
    Ladder,
    LadderOps,
    LadderOptions,
    LadderProposal,
    VideoRendition,
    propose,
)

REPO = Path(__file__).resolve().parents[2]
STANDARD_VERSION = "standard@843ed1d05a"
"""Changing it re-provisions every kit and re-encodes every asset: update it only with a deliberate ladder change."""


def _with(ladder: Ladder, **changes: Any) -> dict[str, Any]:
    return ladder.model_dump(mode="json") | changes


def _rung(ladder: Ladder, rung: str, **changes: Any) -> dict[str, Any]:
    return next(r for r in ladder.video if r.name == rung).model_dump(mode="json") | changes


def _rungs(ladder: Ladder) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in ladder.video]


def test_standard_is_three_avc_rungs_with_muxed_aac(standard: Ladder) -> None:
    sizes = [(r.name, r.width, r.height, r.qvbr, r.max_kbps, r.hrd_kbps) for r in standard.video]
    assert sizes == [
        ("avc-720p", 1280, 720, 7, 3400, None),  # multivariant order: players start on the 720p rung
        ("avc-1080p", 1920, 1080, 8, 6000, None),
        ("avc-360p", 640, 360, 6, 800, 800),
    ]
    assert {r.profile for r in standard.video} == {"HIGH"}
    assert standard.audio == Audio(kbps=128)
    assert standard.options == LadderOptions(allow_frame_rate_conversion=False)
    assert standard.max_height == 1080
    assert str(standard.frame_rate) == "30000/1001"
    assert (standard.grid.gop_frames, standard.grid.gop_ms, standard.grid.segment_ms) == (60, 2002, 6006)
    assert [r.name_modifier for r in standard.video] == ["_avc-720p", "_avc-1080p", "_avc-360p"]


def test_root_ladders_directory_is_the_package_data() -> None:
    assert (REPO / "ladders").resolve() == BUILTIN_DIR.resolve()
    assert sorted(p.name for p in BUILTIN_DIR.glob("*.toml")) == ["standard.toml"]
    with pytest.raises(FileNotFoundError):
        Ladder.builtin("uhd")


def test_builtin_reads_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standard: Ladder
) -> None:
    (tmp_path / "custom.toml").write_text(standard.model_copy(update={"id": "custom"}).to_toml())
    monkeypatch.setenv(LADDERS_DIR_ENV, str(tmp_path))
    assert Ladder.builtin("custom").fingerprint == standard.fingerprint
    with pytest.raises(ValueError, match="invalid ladder name"):
        Ladder.builtin("../etc/passwd")


def test_the_standard_fingerprint_is_stable(standard: Ladder) -> None:
    assert standard.version == STANDARD_VERSION
    assert re.fullmatch(r"standard@[0-9a-f]{10}", standard.version)
    assert Ladder.model_validate(standard.model_dump(mode="json")).version == STANDARD_VERSION
    assert Ladder.model_validate(_with(standard, gop_seconds=2, segment_seconds=6)).version == STANDARD_VERSION
    assert Ladder.model_validate(_with(standard, frame_rate="29.97")).version == STANDARD_VERSION
    renamed = standard.model_copy(update={"id": "house", "description": "renamed"})
    assert renamed.fingerprint == standard.fingerprint and renamed.version == f"house@{standard.fingerprint}"


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(lambda lad: {"video": [r | {"qvbr": 5} for r in _rungs(lad)]}, id="qvbr"),
        pytest.param(lambda lad: {"video": [_rung(lad, "avc-720p", max_kbps=3500), *_rungs(lad)[1:]]}, id="bitrate"),
        pytest.param(lambda lad: {"video": [r | {"profile": "MAIN"} for r in _rungs(lad)]}, id="profile"),
        pytest.param(lambda lad: {"video": _rungs(lad)[::-1]}, id="variant-order"),
        pytest.param(lambda lad: {"video": _rungs(lad)[:2]}, id="rung-dropped"),
        pytest.param(lambda lad: {"audio": {"kbps": 96}}, id="audio"),
        pytest.param(lambda lad: {"frame_rate": "25"}, id="frame-rate"),
        pytest.param(lambda lad: {"segment_seconds": 4}, id="segments"),
        pytest.param(lambda lad: {"options": {"allow_frame_rate_conversion": True}}, id="options"),
    ],
)
def test_the_fingerprint_changes_with_the_encode(standard: Ladder, changes: Any) -> None:
    assert Ladder.model_validate(_with(standard, **changes(standard))).fingerprint != standard.fingerprint


def test_serialisation_round_trips(standard: Ladder, tmp_path: Path) -> None:
    assert Ladder.model_validate(standard.model_dump(mode="json")) == standard
    assert Ladder.model_validate_json(standard.model_dump_json()) == standard
    path = tmp_path / "standard.toml"
    path.write_text(standard.to_toml())
    assert Ladder.load(path) == standard
    text = standard.to_toml()
    assert 'frame_rate = "30000/1001"' in text and "[audio]\nkbps = 128\n" in text
    assert "hrd_kbps = 800" in text and text.count("hrd_kbps") == 1  # None is left out


def test_nominal_bandwidth_is_peak_video_plus_audio(standard: Ladder) -> None:
    nominal = {r.name: standard.nominal_bandwidth(r) for r in standard.video}
    assert nominal == {
        "avc-720p": (3_740_000 + 128_000, 3_400_000 + 128_000),
        "avc-1080p": (6_600_000 + 128_000, 6_000_000 + 128_000),
        "avc-360p": (880_000 + 128_000, 800_000 + 128_000),
    }
    louder = Ladder.model_validate(_with(standard, audio={"kbps": 192}))
    assert louder.nominal_bandwidth(louder.video[0]) == (3_740_000 + 192_000, 3_400_000 + 192_000)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda lad: {"video": [_rung(lad, "avc-720p"), *_rungs(lad)]}, r"duplicate rendition names \['avc-720p'\]"),
        (lambda lad: {"video": []}, "at least 1 item"),
        (lambda lad: {"segment_seconds": 5}, "multiple of gop_seconds"),
        (lambda lad: {"gop_seconds": 1.5, "segment_seconds": 4.5}, "whole seconds"),
        (lambda lad: {"segment_seconds": 6.006}, "whole seconds"),
        (lambda lad: {"frame_rate": "120"}, "exceeds 60 fps"),
        (lambda lad: {"frame_rate": "12.5", "gop_seconds": 1, "segment_seconds": 6}, "whole number of frames"),
        (
            lambda lad: {
                "video": [_rung(lad, "avc-360p", hrd_kbps=None) if r["height"] == 360 else r for r in _rungs(lad)]
            },
            "avc-360p: hrd_kbps must equal max_kbps at <= 432p",
        ),
        (
            lambda lad: {
                "video": [*_rungs(lad), _rung(lad, "avc-360p", name="avc-432p", width=768, height=432, hrd_kbps=1)]
            },
            "avc-432p: hrd_kbps must equal",
        ),
        (
            lambda lad: {"video": [*_rungs(lad), _rung(lad, "avc-1080p", name="avc-4k", width=3840, height=2160)]},
            "1080",
        ),
        (lambda lad: {"video": [*_rungs(lad), _rung(lad, "avc-1080p", name="avc-wide", width=2048)]}, "1920"),
        (lambda lad: {"audio": {"kbps": 127}}, "127 kbit/s is not an AAC-LC stereo bitrate"),
        (lambda lad: {"audio": [{"group_id": "aac-lc-128", "kbps": 128}]}, "audio"),  # no audio rendition groups
        (lambda lad: {"captions": [{"language": "en", "label": "English"}]}, "captions"),
        (lambda lad: {"options": {"iframe_playlists": True}}, "iframe_playlists"),
        (lambda lad: {"options": {"loudness_lkfs": -24.0}}, "loudness_lkfs"),
        (lambda lad: {"options": {"acceleration": "PREFERRED"}}, "acceleration"),
    ],
)
def test_ladder_rules(standard: Ladder, mutate: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Ladder.model_validate(_with(standard, **mutate(standard)))


def test_hrd_is_only_required_up_to_432p(standard: Ladder) -> None:
    sd = _rung(standard, "avc-360p", name="avc-480p", width=854, height=480, max_kbps=1500, hrd_kbps=None)
    assert Ladder.model_validate(_with(standard, video=[*_rungs(standard), sd])).video[-1].hrd_kbps is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"width": 3840, "height": 2160}, "less than or equal to 1920"),
        ({"height": 1088}, "less than or equal to 1080"),
        ({"width": 641}, "multiple of 2"),
        ({"height": 64}, "greater than or equal to 96"),
        ({"qvbr": 11}, "less than or equal to 10"),
        ({"profile": "MAIN10"}, "'HIGH' or 'MAIN'"),
        ({"name": "AVC 720p"}, "pattern"),
        ({"codec": "hevc"}, "Extra inputs are not permitted"),  # AVC only
        ({"audio_group": "aac-lc-128"}, "Extra inputs are not permitted"),  # audio is muxed
        ({"bit_depth": 10}, "Extra inputs are not permitted"),  # 8-bit only
    ],
)
def test_rendition_rules(changes: dict[str, Any], message: str) -> None:
    base = {"name": "avc-1080p", "width": 1920, "height": 1080, "qvbr": 8, "max_kbps": 6000}
    assert VideoRendition.model_validate(base).profile == "HIGH"
    with pytest.raises(ValidationError, match=message):
        VideoRendition.model_validate(base | changes)


def test_audio_is_aac_lc_stereo() -> None:
    assert Audio().kbps == 128
    assert [Audio(kbps=k).kbps for k in sorted(AAC_LC_KBPS)] == [64, 80, 96, 112, 128, 160, 192, 224, 256, 288, 320]
    for kbps in (32, 48, 127, 384):  # HE-AAC rates and anything MediaConvert rejects for AAC-LC 2.0
        with pytest.raises(ValidationError, match="not an AAC-LC stereo bitrate"):
            Audio(kbps=kbps)
    with pytest.raises(ValidationError, match="Extra inputs"):
        Audio.model_validate({"kbps": 64, "profile": "he1"})


def test_propose_drops_a_rung(standard: Ladder) -> None:
    proposal = propose(standard, LadderOps(drop_rungs=["avc-1080p"], new_id="sd-hd"))
    assert proposal.errors == [] and proposal.warnings == [] and proposal.ladder is not None
    assert proposal.ladder.version.startswith("sd-hd@") and proposal.ladder.version != standard.version
    assert [r.name for r in proposal.ladder.video] == ["avc-720p", "avc-360p"]
    assert (proposal.removed, proposal.added, proposal.changed) == (["avc-1080p"], [], [])
    assert proposal.cost_per_minute_usd == pytest.approx(0.0375 - 0.015)
    assert proposal.base_version == standard.version
    assert LadderProposal.model_validate(proposal.model_dump(mode="json")) == proposal


def test_propose_quality_shift_and_height_filters(standard: Ladder) -> None:
    proposal = propose(standard, LadderOps(quality_shift=2, max_height=720))
    assert proposal.ladder is not None and proposal.ladder.max_height == 720
    assert [(r.name, r.qvbr) for r in proposal.ladder.video] == [("avc-720p", 9), ("avc-360p", 8)]
    assert (proposal.removed, proposal.changed, proposal.warnings) == (["avc-1080p"], ["avc-720p", "avc-360p"], [])
    hd = propose(standard, LadderOps(min_height=720))
    assert hd.removed == ["avc-360p"] and hd.ladder is not None and hd.ladder.max_height == 1080


def test_propose_clamps_qvbr(standard: Ladder) -> None:
    sharper = propose(standard, LadderOps(quality_shift=2)).ladder
    assert sharper is not None
    clamped = propose(sharper, LadderOps(quality_shift=2))
    assert clamped.warnings == ["avc-720p: QVBR clamped to 10", "avc-1080p: QVBR clamped to 10"]
    assert clamped.changed == ["avc-720p", "avc-360p"]  # avc-1080p was already at 10
    assert clamped.ladder is not None and [r.qvbr for r in clamped.ladder.video] == [10, 10, 10]


def test_propose_reports_invalid_candidates(standard: Ladder) -> None:
    nothing = propose(standard, LadderOps(max_height=100))
    assert (nothing.ladder, nothing.errors, nothing.cost_per_minute_usd) == (None, ["no video renditions left"], 0.0)
    assert nothing.removed == ["avc-1080p", "avc-360p", "avc-720p"]
    same = propose(standard, LadderOps(drop_rungs=["avc-2160p"]))
    assert same.ladder == standard
    assert same.warnings == ["unknown rung avc-2160p", "no change: the proposal equals the base ladder"]
    # A base that skipped validation (model_construct) is rejected but still costed.
    unchecked = Ladder.model_construct(
        **(dict(standard) | {"video": (*standard.video, standard.video[0].model_copy(update={"height": 432}))})
    )
    rejected = propose(unchecked, LadderOps())
    assert rejected.ladder is None
    assert rejected.errors == [
        "duplicate rendition names ['avc-720p']",
        "avc-720p: hrd_kbps must equal max_kbps at <= 432p",
    ]
    assert rejected.cost_per_minute_usd == pytest.approx(0.0375 + 0.0075)


def test_ladder_ops_have_no_codec_or_caption_operations() -> None:
    assert set(LadderOps.model_fields) == {"max_height", "min_height", "quality_shift", "drop_rungs", "new_id"}
    for removed in ("codecs", "captions"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            LadderOps.model_validate({removed: []})
    with pytest.raises(ValidationError):
        LadderOps(quality_shift=3)
