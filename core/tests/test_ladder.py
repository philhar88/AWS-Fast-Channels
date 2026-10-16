import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fast_channels.core.ladder import (
    BUILTIN_DIR,
    LADDERS_DIR_ENV,
    AudioRendition,
    Ladder,
    LadderOps,
    LadderProposal,
    VideoCodec,
    VideoRendition,
    propose,
)

REPO = Path(__file__).resolve().parents[2]


def test_builtin_ladders_match_ladders_md(standard: Ladder, uhd: Ladder) -> None:
    assert [r.name for r in standard.video][:2] == ["avc-720p-a", "avc-1080p-b"]
    assert {c: sum(r.codec is c for r in standard.video) for c in VideoCodec} == {"avc": 8, "hevc": 4, "av1": 4}
    assert [a.group_id for a in standard.audio] == ["aac-lc-128", "aac-he1-64", "aac-he2-32"]
    assert standard.max_height == 1080 and standard.codecs == set(VideoCodec)
    assert {c: sum(r.codec is c for r in uhd.video) for c in VideoCodec} == {"avc": 8, "hevc": 7, "av1": 7}
    assert len(uhd.audio) == 4 and uhd.max_height == 2160
    assert all(r.bit_depth == 10 and r.codec_profile == "MAIN10_MAIN" for r in uhd.video if r.codec is VideoCodec.HEVC)
    assert all(r.bit_depth == 10 for r in standard.video if r.codec is VideoCodec.AV1)
    assert standard.grid.gop_frames == 60 and standard.grid.segment_ms == 6006


def test_root_ladders_directory_is_the_package_data() -> None:
    assert (REPO / "ladders").resolve() == BUILTIN_DIR.resolve()
    assert sorted(p.name for p in BUILTIN_DIR.glob("*.toml")) == ["standard.toml", "uhd.toml"]


def test_builtin_reads_the_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standard: Ladder
) -> None:
    (tmp_path / "custom.toml").write_text(standard.model_copy(update={"id": "custom"}).to_toml())
    monkeypatch.setenv(LADDERS_DIR_ENV, str(tmp_path))
    assert Ladder.builtin("custom").fingerprint == standard.fingerprint
    with pytest.raises(ValueError, match="invalid ladder name"):
        Ladder.builtin("../etc/passwd")


def test_version_is_a_fingerprint_of_encode_settings(standard: Ladder) -> None:
    assert re.fullmatch(r"standard@[0-9a-f]{10}", standard.version)
    assert standard.model_copy(update={"description": "renamed"}).fingerprint == standard.fingerprint
    shifted = standard.model_dump() | {"video": [r.model_dump() | {"qvbr": 5} for r in standard.video]}
    assert Ladder.model_validate(shifted).fingerprint != standard.fingerprint


def test_serialisation_round_trips(standard: Ladder, uhd: Ladder, tmp_path: Path) -> None:
    for ladder in (standard, uhd):
        assert Ladder.model_validate(ladder.model_dump(mode="json")) == ladder
        path = tmp_path / f"{ladder.id}.toml"
        path.write_text(ladder.to_toml())
        assert Ladder.load(path) == ladder
    assert 'frame_rate = "30000/1001"' in standard.to_toml()


def test_nominal_bandwidth_and_tier(standard: Ladder) -> None:
    cellular = next(r for r in standard.video if r.name == "avc-234p")
    assert standard.nominal_bandwidth(cellular) == (132_000 + 32_000, 120_000 + 32_000)  # <= 192 kbit/s
    assert standard.audio_for(cellular).group_id == "aac-he2-32"
    assert standard.pricing_tier() == "PROFESSIONAL"


def _with(ladder: Ladder, **changes: Any) -> dict[str, Any]:
    return ladder.model_dump(mode="json") | changes


def _rung(ladder: Ladder, rung: str, **changes: Any) -> dict[str, Any]:
    return next(r for r in ladder.video if r.name == rung).model_dump(mode="json") | changes


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda lad: {"video": [_rung(lad, "avc-720p-a"), *[r.model_dump() for r in lad.video]]},
            "duplicate rendition",
        ),
        (lambda lad: {"video": [_rung(lad, "avc-720p-a", audio_group="aac-xx")]}, "unknown audio groups"),
        (lambda lad: {"video": [_rung(lad, "avc-720p-a"), _rung(lad, "avc-234p")]}, "not used by any video"),
        (lambda lad: {"segment_seconds": 5}, "multiple of gop_seconds"),
        (lambda lad: {"gop_seconds": 1.5, "segment_seconds": 4.5}, "whole seconds"),
        (lambda lad: {"frame_rate": "120"}, "exceeds 60 fps"),
        (lambda lad: {"frame_rate": "12.5", "gop_seconds": 1, "segment_seconds": 6}, "whole number of frames"),
        (
            lambda lad: {
                "options": {"film_grain": True},
                "video": [r.model_dump() for r in lad.video if r.codec != "av1"],
            },
            "film_grain",
        ),
        (lambda lad: {"video": [r.model_dump() for r in reversed(lad.video)]}, "lowest AVC 720p"),
        (lambda lad: {"video": [r.model_dump() for r in lad.video if r.name != "hevc-360p"]}, "hls.js"),
        (
            lambda lad: {
                "video": [
                    _rung(lad, "avc-360p", hrd_kbps=None) if r.name == "avc-360p" else r.model_dump() for r in lad.video
                ]
            },
            "hrd_kbps must equal",
        ),
        (
            lambda lad: {
                "video": [
                    *[r.model_dump() for r in lad.video],
                    _rung(lad, "av1-1080p", name="av1-4k", width=4096, height=2304),
                ]
            },
            "4096x2160",
        ),
        (
            lambda lad: {
                "video": [
                    *[r.model_dump() for r in lad.video],  # 4096x2176@60 is exactly at the 5.1 limits
                    _rung(lad, "hevc-1080p", name="hevc-4k", width=4096, height=2304),
                ]
            },
            "HEVC level 5.1",
        ),
    ],
)
def test_ladder_rules(standard: Ladder, mutate: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Ladder.model_validate(_with(standard, **mutate(standard)))


def test_rendition_rules() -> None:
    base = {"name": "x", "width": 640, "height": 360, "qvbr": 6, "max_kbps": 300, "audio_group": "a"}
    with pytest.raises(ValidationError, match="does not support 10-bit"):
        VideoRendition.model_validate(base | {"codec": "avc", "bit_depth": 10})
    with pytest.raises(ValidationError, match="profile MAIN_MAIN is invalid"):
        VideoRendition.model_validate(base | {"codec": "hevc", "bit_depth": 10, "profile": "MAIN_MAIN"})
    with pytest.raises(ValidationError, match="no HRD"):
        VideoRendition.model_validate(base | {"codec": "av1", "hrd_kbps": 300})
    assert VideoRendition.model_validate(base | {"codec": "av1"}).codec_profile is None
    assert VideoRendition.model_validate(base | {"codec": "avc", "profile": "MAIN"}).codec_profile == "MAIN"
    with pytest.raises(ValidationError, match="not a valid he1"):
        AudioRendition(group_id="aac-he1-96", profile="he1", kbps=96)  # type: ignore[arg-type]


def test_propose_drop_codec(standard: Ladder) -> None:
    proposal = propose(standard, LadderOps(codecs=[VideoCodec.AVC, VideoCodec.HEVC], new_id="no-av1"))
    assert proposal.errors == [] and proposal.ladder is not None
    assert proposal.ladder.version.startswith("no-av1@")
    assert proposal.removed == ["av1-1080p", "av1-360p", "av1-540p", "av1-720p"]
    assert proposal.added == [] and proposal.changed == []
    assert proposal.cost_per_minute_usd == pytest.approx(0.56 - 0.252, abs=1e-3)
    assert proposal.base_version == standard.version
    assert LadderProposal.model_validate(proposal.model_dump(mode="json")) == proposal


def test_propose_quality_shift_and_height_filters(standard: Ladder) -> None:
    proposal = propose(standard, LadderOps(quality_shift=2, max_height=720, min_height=200))
    assert proposal.ladder is not None
    assert proposal.ladder.max_height == 720
    assert proposal.removed == ["av1-1080p", "avc-1080p-a", "avc-1080p-b", "hevc-1080p"]
    assert "avc-720p-a" in proposal.changed
    assert proposal.warnings == []
    clamped = propose(standard, LadderOps(quality_shift=2))
    assert "avc-1080p-b: QVBR clamped to 10" in clamped.warnings


def test_propose_reports_invalid_candidates(standard: Ladder) -> None:
    no_avc = propose(standard, LadderOps(codecs=[VideoCodec.HEVC]))
    assert no_avc.ladder is None
    assert any("AVC 720p" in e for e in no_avc.errors)
    assert no_avc.cost_per_minute_usd > 0
    assert "aac-he2-32" in no_avc.removed  # audio no remaining rung uses is pruned
    no_floor = propose(standard, LadderOps(min_height=300))  # the 234p rung is AVC's only one <= 500 kbit/s
    assert no_floor.errors == ["lowest avc variant is 724000 bit/s; hls.js needs one <= 500000"]
    nothing = propose(standard, LadderOps(max_height=100))
    assert (nothing.errors, nothing.cost_per_minute_usd) == (["no video renditions left"], 0.0)
    same = propose(standard, LadderOps(drop_rungs=["avc-9999p"]))
    assert same.warnings == ["unknown rung avc-9999p", "no change: the proposal equals the base ladder"]
