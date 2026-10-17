from pathlib import Path
from typing import Final

from fast_channels.core.ladder import Ladder
from fast_channels.functions import encode_timeline
from fast_channels.functions.ingest.finalize import Encode, break_offsets

SAMPLES: Final = Path(__file__).parent / "ingest/data/content"  # a real STANDARD TS encode: 40.073 s


def playlist(*durations: str) -> str:
    segments = "".join(f"#EXTINF:{d},\nseg{n}.ts\n" for n, d in enumerate(durations))
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n{segments}#EXT-X-ENDLIST\n"


MULTIVARIANT: Final = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=3878000,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720
avc_720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6728000,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080
avc_1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1008000,CODECS="avc1.64001e,mp4a.40.2",RESOLUTION=640x360
avc_360.m3u8
"""


def test_every_variant_counts_reference_first() -> None:
    assert encode_timeline.timeline_uris(MULTIVARIANT) == ["avc_720.m3u8", "avc_1080.m3u8", "avc_360.m3u8"]


def test_a_variant_listed_twice_is_read_once() -> None:
    twice = MULTIVARIANT + '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.64001e,mp4a.40.2"\navc_360.m3u8\n'

    assert encode_timeline.timeline_uris(twice) == ["avc_720.m3u8", "avc_1080.m3u8", "avc_360.m3u8"]


def test_breaks_go_only_where_every_variant_has_a_boundary() -> None:
    playlists = {
        "avc_720.m3u8": playlist("6.006", "6.006", "6.006", "6.006"),
        "avc_1080.m3u8": playlist("6.006", "6.006", "6.006", "6.006"),
        "avc_360.m3u8": playlist("12.012", "6.006", "6.006"),  # segmented differently: no boundary at 6 s
    }

    result = encode_timeline.timeline(MULTIVARIANT, playlists.__getitem__)

    assert result == encode_timeline.Timeline(duration_ms=24_024, boundaries_ms=[12_012, 18_018])


def test_duration_is_the_exact_sum_of_the_reference_playlist() -> None:
    assert encode_timeline.duration_ms(playlist(*["6.006"] * 1000)) == 6_006_000  # a float sum drifts


def test_the_pipeline_and_the_register_tool_read_the_same_variants() -> None:
    # finalize.break_offsets (pipeline) and origins (register_vod_source) both go through encode_timeline.
    main = (SAMPLES / "main.m3u8").read_text()
    media = {path.name: path.read_text() for path in SAMPLES.glob("main_*.m3u8")}
    encode = Encode(ladder=Ladder.builtin("standard"), prefix="", main=main, media=media)

    shared = encode_timeline.timeline(main, media.__getitem__)

    assert break_offsets(encode, []) == shared.boundaries_ms == [6006, 12012, 18018, 24024, 30030, 36036]
    assert shared.duration_ms == 40_073
    assert encode_timeline.timeline_uris(main) == ["main_avc-720p.m3u8", "main_avc-1080p.m3u8", "main_avc-360p.m3u8"]
