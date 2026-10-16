"""Manifest readers and rewriters on real MediaConvert, Channel Assembly and MediaTailor playlists.

golden-job/: the e2e golden encode (30 fps, ESAM at 20/40 s). ladders-2997/: the ladders research encode
(29.97 fps, I-frame playlists). standard-job/: the compiled STANDARD template run on a slate and on 1080i25
with an off-grid cue point. channel-assembly/: the CA channel multivariant, an SSAI media playlist and a
MediaTailor-rehosted preconditioned ad from the e2e run.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fast_channels.core import manifests as m
from fast_channels.core.profiles import applicable, profile

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

DATA = Path(__file__).parent / "data"


def read(relative: str) -> str:
    return (DATA / relative).read_text()


def test_parse_multivariant() -> None:
    variants = m.parse_multivariant(read("golden-job/index.m3u8"))
    assert len(variants) == 16
    first = variants[0]
    assert first == m.Variant(
        uri="index_av1_1080p.m3u8",
        bandwidth=4396128,
        average_bandwidth=4236561,
        codecs=("av01.0.08M.10", "mp4a.40.2"),
        resolution=(1920, 1080),
        frame_rate=30.0,
        audio_group="aac-lc-128k",
    )
    assert [v.video_codec for v in variants[::2]] == ["av1"] * 3 + ["hevc"] * 2 + ["avc"] * 3
    assert m.Variant("a.m3u8", 1, None, ("mp4a.40.2",), None, None, None).video_codec is None


def test_channel_assembly_preserves_the_ladder_signature() -> None:
    # CA re-authors the multivariant (attribute order, FRAME-RATE=30.0, child URIs) but keeps the ladder.
    assert m.ladder_signature(read("channel-assembly/ca-main-multivariant-sigv4.m3u8")) == m.ladder_signature(
        read("golden-job/index.m3u8")
    )
    assert m.ladder_signature(read("golden-job/index.m3u8"))[0] == ("av1", "1920x1080", "30.000", "aac-lc-128k")
    # MediaTailor re-hosts a preconditioned ad's multivariant verbatim: it must carry the content ladder.
    assert m.ladder_signature(read("channel-assembly/preconditioned-ads-rehosted-index.m3u8")) == m.ladder_signature(
        read("golden-job/index.m3u8")
    )


@pytest.mark.parametrize("asset", ["slate", "src25i"])
def test_standard_encode_matches_the_expected_signature_per_profile(standard: Ladder, asset: str) -> None:
    for p in applicable(standard):
        if p.format == "HLS":
            text = read(f"standard-job/{asset}/{Path(p.manifest_key('')).name}")
            assert m.ladder_signature(text) == m.expected_signature(standard, p), p.name
    assert m.expected_signature(standard) == m.ladder_signature(read(f"standard-job/{asset}/main.m3u8"))


def test_media_segment_durations() -> None:
    assert m.media_segment_durations(read("golden-job/index_avc_1080p.m3u8")) == [6.0] * 3 + [2.0] * 2 + [6.0] * 6 + [
        2.0
    ]
    assert m.media_segment_durations(read("ladders-2997/main_avc-720p-a.m3u8")) == [6.006] * 3 + [1.969]


def test_segment_boundaries_common_to_every_track() -> None:
    video = [read(f"golden-job/index_{r}.m3u8") for r in ("avc_1080p", "hevc_1080p", "av1_1080p")]
    audio = [read("golden-job/index_aac-lc-128k.m3u8"), read("golden-job/index_he-aac-64k.m3u8")]
    assert m.segment_boundaries_ms(*video, *audio) == [
        6000,
        12000,
        18000,
        20000,
        22000,
        28000,
        34000,
        40000,
        46000,
        52000,
        58000,
    ]
    assert 20000 not in m.segment_boundaries_ms(*video, *audio, tolerance_ms=60)  # HE-AAC trails by ~96 ms
    ntsc = [
        read(f"ladders-2997/main_{r}.m3u8")
        for r in ("avc-720p-a", "av1-360p", "hevc-360p", "aac-lc-128", "aac-he1-64", "aac-he2-32")
    ]
    assert m.segment_boundaries_ms(*ntsc) == [6006, 12012, 18018]  # HE-AACv2 trails by up to 148 ms
    esam = [read(f"standard-job/src25i/main_{r}.m3u8") for r in ("avc-720p-a", "av1-360p", "aac-he2-32")]
    assert m.segment_boundaries_ms(*esam) == [4004]
    assert m.segment_boundaries_ms() == []


def test_nominal_bandwidths_and_normalisation(standard: Ladder) -> None:
    text = read("standard-job/slate/main.m3u8")
    nominal = m.nominal_bandwidths(standard, text)
    assert len(nominal) == 16
    assert nominal["main_avc-720p-a.m3u8"] == (3_740_000 + 128_000, 3_400_000 + 128_000)
    normalised = m.normalize_bandwidth(text, nominal)
    rewritten = {v.uri: (v.bandwidth, v.average_bandwidth) for v in m.parse_multivariant(normalised)}
    assert rewritten == nominal
    assert m.ladder_signature(normalised) == m.ladder_signature(text)
    assert m.normalize_bandwidth(normalised, nominal) == normalised
    assert normalised.count("\n") == text.count("\n")
    assert m.nominal_bandwidths(standard, read("channel-assembly/ca-main-multivariant-sigv4.m3u8")) == {}


def test_normalisation_adds_a_missing_average_bandwidth() -> None:
    text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001f",RESOLUTION=1280x720\nv.m3u8\n'
    assert (
        m.normalize_bandwidth(text, {"v.m3u8": (5, 4)}).splitlines()[1]
        == '#EXT-X-STREAM-INF:BANDWIDTH=5,CODECS="avc1.64001f",RESOLUTION=1280x720,AVERAGE-BANDWIDTH=4'
    )


def test_bump_iframe_version() -> None:
    text = read("ladders-2997/main_avc-720p-a_I-Frame.m3u8")
    bumped = m.bump_iframe_version(text)
    assert "#EXT-X-VERSION:5" in bumped and "#EXT-X-VERSION:4" not in bumped
    assert m.bump_iframe_version(bumped) == bumped
    media = read("ladders-2997/main_avc-720p-a.m3u8")
    assert m.bump_iframe_version(media) == media


def test_absolutize_media_playlist() -> None:
    base = "https://cdn.example.com/ads/standard_0123abcd45/house-30/"
    out = m.absolutize_media_playlist(read("golden-job/index_avc_1080p.m3u8"), base)
    assert f'#EXT-X-MAP:URI="{base}index_avc_1080pinit.cmfv"' in out
    segments = [line for line in out.splitlines() if line and not line.startswith("#")]
    assert len(segments) == 12 and all(s.startswith(base) for s in segments)
    stitched = read("channel-assembly/ssai-pre-avc-media.m3u8")  # SSAI output: already absolute
    assert m.absolutize_media_playlist(stitched, base).splitlines() == stitched.splitlines()


def test_check_hls_on_the_standard_encode() -> None:
    findings = m.check_hls(read("standard-job/slate/main.m3u8"))
    assert findings and all(f.startswith("no I-frame playlist") for f in findings)  # I-frames are off by default


def test_check_hls_on_research_encodes() -> None:
    golden = m.check_hls(read("golden-job/index.m3u8"))
    assert "first variant should be the ~2 Mbit/s AVC 720p rung (Apple 1.32)" in golden
    assert "no variant has BANDWIDTH <= 192 kbit/s (iOS cellular)" in golden
    assert "lowest hevc variant is above 500 kbit/s (hls.js start rule)" in golden
    assert "audio group aac-lc-128k: LANGUAGE 'eng' should be a 2-letter BCP-47 code" in golden
    ntsc = m.check_hls(read("ladders-2997/main.m3u8"))
    assert not any(f.startswith("no I-frame") for f in ntsc)
    ca = m.check_hls(read("channel-assembly/ca-main-multivariant-sigv4.m3u8"))
    assert "EXT-X-INDEPENDENT-SEGMENTS must appear exactly once" in ca
    assert any("FRAME-RATE should have 3 decimals" in f for f in ca)


def test_check_hls_structure_rules() -> None:
    assert m.check_hls("") == ["playlist does not start with #EXTM3U"]
    assert m.check_hls("#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n") == ["no EXT-X-STREAM-INF variants"]
    text = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="en",NAME="English",CHANNELS="2",DEFAULT=YES',
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES',
            '#EXT-X-STREAM-INF:BANDWIDTH=100000,CODECS="avc1.64001f",RESOLUTION=1280x720,AUDIO="a",SCORE=1',
            "v1.m3u8",
            '#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1920x1080,AUDIO="a"',
            "v2.m3u8",
            '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="i.m3u8"',
        ]
    )
    findings = m.check_hls(text)
    assert "v1.m3u8: missing AVERAGE-BANDWIDTH, FRAME-RATE, VIDEO-RANGE" in findings
    assert "v1.m3u8: CODECS lacks the audio codec of group a" in findings
    assert "SCORE must be on every variant or none" in findings
    assert "fewer than 2 variants at the top resolution (Apple 9.10)" in findings
    assert "I-frame variants need CODECS and RESOLUTION" in findings
    assert "audio group a: DEFAULT=YES needs AUTOSELECT=YES" in findings
    assert "audio group a: NAME and CHANNELS are required" in findings
    assert "audio group a: exactly one rendition must be DEFAULT=YES" in findings
    assert profile("hls").name == "hls"
