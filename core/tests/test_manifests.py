"""Manifest readers and rewriters on real MediaConvert output and on Channel Assembly / MediaTailor playlists.

hls-ts/content/ and hls-ts/slate/: what the compiled STANDARD template wrote for a 40 s clip and a 15-GOP still-image
slate (mediastreamvalidator: PASS). hls-ts/channel-assembly/: derived from that capture in the shape Channel Assembly
and MediaTailor wrote for the e2e run (re-authored channel multivariant; an SSAI media playlist with absolute CDN
segment URIs, discontinuities and a DATERANGE break); placeholders, no account ids, until a live TS recapture.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from fast_channels.core import manifests as m

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

DATA = Path(__file__).parent / "data/hls-ts"
RUNGS = ("avc-720p", "avc-1080p", "avc-360p")


def read(relative: str) -> str:
    return (DATA / relative).read_text()


def variants(asset: str) -> list[str]:
    """The media playlists of a captured encode, in multivariant order."""
    return [read(f"{asset}/main_{rung}.m3u8") for rung in RUNGS]


CONTENT = read("content/main.m3u8")
SLATE = read("slate/main.m3u8")
CHANNEL = read("channel-assembly/channel.m3u8")
SSAI = read("channel-assembly/ssai-avc-720p.m3u8")


def test_parse_multivariant() -> None:
    parsed = m.parse_multivariant(CONTENT)
    assert [v.uri for v in parsed] == [f"main_{rung}.m3u8" for rung in RUNGS]
    assert parsed[0] == m.Variant(
        uri="main_avc-720p.m3u8",
        bandwidth=1639475,
        average_bandwidth=1248617,
        codecs=("avc1.64001f", "mp4a.40.2"),
        resolution=(1280, 720),
        frame_rate=29.97,
    )
    assert [(v.video_codec, v.audio_codec) for v in parsed] == [("avc", m.AUDIO_CODEC)] * 3
    assert [v.codecs[0] for v in parsed] == ["avc1.64001f", "avc1.640028", "avc1.64001e"]  # High 3.1, 4.0, 3.0
    assert m.Variant("a.m3u8", 1, None, ("hvc1.2.4.L120.B0",), None, None).video_codec == "hevc"
    assert m.Variant("a.m3u8", 1, None, (), None, None).audio_codec is None


def test_attributes() -> None:
    line = CONTENT.splitlines()[3]
    assert m.attributes(line) == {
        "BANDWIDTH": "1639475",
        "AVERAGE-BANDWIDTH": "1248617",
        "VIDEO-RANGE": "SDR",
        "CODECS": "avc1.64001f,mp4a.40.2",
        "RESOLUTION": "1280x720",
        "FRAME-RATE": "29.970",
        "CLOSED-CAPTIONS": "NONE",
    }


def test_every_encode_matches_the_expected_signature(standard: Ladder) -> None:
    expected = m.expected_signature(standard)
    assert expected == (
        ("avc", "1280x720", "29.970", "mp4a.40.2"),
        ("avc", "1920x1080", "29.970", "mp4a.40.2"),
        ("avc", "640x360", "29.970", "mp4a.40.2"),
    )
    # Content and slates share one source group, so their signatures must be identical.
    assert m.ladder_signature(CONTENT) == m.ladder_signature(SLATE) == expected


def test_channel_assembly_preserves_the_ladder_signature() -> None:
    # CA re-authors the multivariant (attribute order, FRAME-RATE=29.97, positional child URIs) but keeps the ladder.
    assert m.ladder_signature(CHANNEL) == m.ladder_signature(CONTENT)


def test_signature_changes_when_the_ladder_does(standard: Ladder) -> None:
    expected = m.expected_signature(standard)
    lines = CONTENT.splitlines(keepends=True)
    swapped = "".join([*lines[:3], *lines[5:7], *lines[3:5], *lines[7:]])
    assert m.ladder_signature(swapped) == (expected[1], expected[0], expected[2])  # CA matches by position
    demuxed = CONTENT.replace(',mp4a.40.2"', '"', 1)
    assert m.ladder_signature(demuxed)[0] == ("avc", "1280x720", "29.970", "")
    resized = CONTENT.replace("RESOLUTION=1280x720", "RESOLUTION=1024x576")
    assert m.ladder_signature(resized)[0] == ("avc", "1024x576", "29.970", "mp4a.40.2")
    pal = CONTENT.replace("FRAME-RATE=29.970", "FRAME-RATE=25.000")
    assert {row[2] for row in m.ladder_signature(pal)} == {"25.000"}
    assert expected not in {m.ladder_signature(text) for text in (swapped, demuxed, resized, pal)}


def test_media_playlists_are_plain_ts() -> None:
    for text in (*variants("content"), *variants("slate")):
        assert "#EXT-X-MAP" not in text and "#EXT-X-VERSION:3" in text and "#EXT-X-TARGETDURATION:6" in text
        uris = [line for line in text.splitlines() if line and not line.startswith("#")]
        assert all(u.endswith(".ts") for u in uris)


def test_media_segment_durations() -> None:
    assert [m.media_segment_durations(t) for t in variants("content")] == [[6.006] * 6 + [4.037]] * 3
    assert [m.media_segment_durations(t) for t in variants("slate")] == [[6.006] * 5] * 3  # 15 GOPs = 5 segments
    assert m.media_segment_durations(SSAI) == [6.006] * 12
    assert m.media_segment_durations(CONTENT) == []


def test_segment_boundaries_are_shared_by_every_variant(standard: Ladder) -> None:
    content = m.segment_boundaries_ms(*variants("content"))
    assert content == [6006, 12012, 18018, 24024, 30030, 36036]  # exact: no float drift, no final boundary
    assert all(b % standard.grid.segment_ms == 0 for b in content)
    assert m.segment_boundaries_ms(*variants("slate")) == [6006, 12012, 18018, 24024]
    assert m.segment_boundaries_ms(variants("content")[0]) == content
    assert m.segment_boundaries_ms() == []


def test_segment_boundaries_need_an_exact_match() -> None:
    reference, *others = variants("content")
    late = reference.replace("#EXTINF:6.006,", "#EXTINF:6.007,", 1)  # 1 ms off shifts every later boundary
    assert m.segment_boundaries_ms(late, *others) == []
    split = reference.replace("#EXTINF:6.006,\nmain_avc-720p_00002.ts", "#EXTINF:4.004,\na.ts\n#EXTINF:2.002,\nb.ts")
    assert m.segment_boundaries_ms(split, *others) == [6006, 12012, 18018, 24024, 30030, 36036]
    assert m.segment_boundaries_ms(*others, split) == [6006, 12012, 18018, 24024, 30030, 36036]
    assert 10010 in m.segment_boundaries_ms(split)


def test_nominal_bandwidths_and_normalisation(standard: Ladder) -> None:
    nominal = m.nominal_bandwidths(standard, SLATE)
    assert nominal == {
        "main_avc-720p.m3u8": (3_868_000, 3_528_000),
        "main_avc-1080p.m3u8": (6_728_000, 6_128_000),
        "main_avc-360p.m3u8": (1_008_000, 928_000),
    }
    normalised = m.normalize_bandwidth(SLATE, nominal)
    assert {v.uri: (v.bandwidth, v.average_bandwidth) for v in m.parse_multivariant(normalised)} == nominal
    assert normalised.splitlines()[3] == (
        "#EXT-X-STREAM-INF:BANDWIDTH=3868000,AVERAGE-BANDWIDTH=3528000,VIDEO-RANGE=SDR,"
        'CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=29.970,CLOSED-CAPTIONS=NONE'
    )
    assert m.ladder_signature(normalised) == m.ladder_signature(SLATE)
    assert m.normalize_bandwidth(normalised, nominal) == normalised
    assert m.normalize_bandwidth(m.normalize_bandwidth(CONTENT, nominal), nominal) == normalised  # content == slate
    assert normalised.count("\n") == SLATE.count("\n")
    assert m.nominal_bandwidths(standard, CHANNEL) == {}  # positional CA child URIs name no rendition


def test_normalisation_adds_a_missing_average_bandwidth() -> None:
    text = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720\nv.m3u8\n'
    assert (
        m.normalize_bandwidth(text, {"v.m3u8": (5, 4)}).splitlines()[1]
        == '#EXT-X-STREAM-INF:BANDWIDTH=5,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,AVERAGE-BANDWIDTH=4'
    )


def test_check_hls_passes_the_capture_and_the_channel() -> None:
    assert m.check_hls(CONTENT) == []
    assert m.check_hls(SLATE) == []
    assert m.check_hls(CHANNEL) == [f"hls/{n}.m3u8: FRAME-RATE should have 3 decimals" for n in (1, 2, 3)]


def _first(pattern: str, replacement: str) -> str:
    """CONTENT with ``pattern`` in the first variant's EXT-X-STREAM-INF replaced by ``replacement``."""
    line = CONTENT.splitlines()[3]
    broken = re.sub(pattern, replacement, line, count=1)
    assert broken != line, pattern
    return CONTENT.replace(line, broken)


GROUP_FINDING = "main_avc-720p.m3u8: references a rendition group; audio is muxed and there are no subtitles"


@pytest.mark.parametrize(
    ("broken", "findings"),
    [
        (_first(r",RESOLUTION=[^,]*", ""), ["main_avc-720p.m3u8: missing RESOLUTION"]),
        (_first(r"(?<=:)BANDWIDTH=\d+,", ""), ["main_avc-720p.m3u8: missing BANDWIDTH"]),
        (
            _first(r',CODECS="[^"]*"', ""),
            ["main_avc-720p.m3u8: missing CODECS", "main_avc-720p.m3u8: CODECS lacks the muxed audio codec"],
        ),
        (_first(r',mp4a\.40\.2"', '"'), ["main_avc-720p.m3u8: CODECS lacks the muxed audio codec"]),  # demuxed
        (_first("FRAME-RATE=29.970", "FRAME-RATE=29.97"), ["main_avc-720p.m3u8: FRAME-RATE should have 3 decimals"]),
        (_first("VIDEO-RANGE=SDR", 'VIDEO-RANGE=SDR,AUDIO="aac"'), [GROUP_FINDING]),
        (_first("VIDEO-RANGE=SDR", 'VIDEO-RANGE=SDR,SUBTITLES="subs"'), [GROUP_FINDING]),
        (_first("VIDEO-RANGE=SDR", "VIDEO-RANGE=SDR,SCORE=1.0"), ["SCORE must be on every variant or none"]),
    ],
)
def test_check_hls_reports_broken_variants(broken: str, findings: list[str]) -> None:
    assert m.check_hls(broken) == findings


def test_check_hls_structure() -> None:
    assert m.check_hls("") == ["playlist does not start with #EXTM3U"]
    assert m.check_hls(CONTENT.removeprefix("#EXTM3U\n")) == ["playlist does not start with #EXTM3U"]
    assert m.check_hls("#EXTM3U\n#EXT-X-INDEPENDENT-SEGMENTS\n") == ["no EXT-X-STREAM-INF variants"]
    assert m.check_hls(read("content/main_avc-720p.m3u8")) == ["no EXT-X-STREAM-INF variants"]  # a media playlist
    scored = CONTENT.replace("VIDEO-RANGE=SDR,", "VIDEO-RANGE=SDR,SCORE=1.0,")
    assert m.check_hls(scored) == []  # SCORE on every variant
