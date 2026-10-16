from pathlib import Path
from typing import Final

from fast_channels.functions import encode_timeline
from fast_channels.functions.ingest.finalize import Encode, break_offsets

REPO: Final = Path(__file__).resolve().parents[2]
SAMPLES: Final = REPO / "core/tests/data/standard-job/src25i"  # a real STANDARD encode: 10 s, ESAM split at 4004 ms


def playlist(*durations: str) -> str:
    segments = "".join(f"#EXTINF:{d},\nseg{n}.cmfv\n" for n, d in enumerate(durations))
    return f"#EXTM3U\n#EXT-X-TARGETDURATION:6\n{segments}#EXT-X-ENDLIST\n"


MULTIVARIANT: Final = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="en",LANGUAGE="en",URI="audio_en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aac",NAME="fr",LANGUAGE="fr",URI="audio_fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=3000000,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,AUDIO="aac"
avc_720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,CODECS="avc1.64001e,mp4a.40.2",RESOLUTION=640x360,AUDIO="aac"
avc_360.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,CODECS="hvc1.1.6.L93.B0,mp4a.40.2",RESOLUTION=1280x720,AUDIO="aac"
hevc_720.m3u8
"""


def test_one_playlist_per_video_codec_and_every_audio_rendition() -> None:
    assert encode_timeline.timeline_uris(MULTIVARIANT) == [
        "avc_720.m3u8",
        "hevc_720.m3u8",
        "audio_en.m3u8",
        "audio_fr.m3u8",  # a second rendition of the same group still counts
    ]


def test_breaks_go_only_where_every_rendition_has_a_boundary() -> None:
    playlists = {
        "avc_720.m3u8": playlist("6.006", "6.006", "6.006", "6.006"),
        "hevc_720.m3u8": playlist("6.006", "6.006", "6.006", "6.006"),
        "audio_en.m3u8": playlist("6.037", "6.016", "6.016", "5.951"),
        "audio_fr.m3u8": playlist("12.053", "6.016", "5.951"),  # segmented differently: no boundary at 6 s
    }

    result = encode_timeline.timeline(MULTIVARIANT, playlists.__getitem__)

    assert result == encode_timeline.Timeline(duration_ms=24_024, boundaries_ms=[12_012, 18_018])


def test_duration_is_the_exact_sum_of_the_reference_playlist() -> None:
    assert encode_timeline.duration_ms(playlist(*["6.006"] * 1000)) == 6_006_000  # a float sum drifts


def test_the_pipeline_and_the_register_tool_read_the_same_tracks() -> None:
    # finalize.break_offsets (pipeline) and origins (register_vod_source) both go through encode_timeline.
    main = (SAMPLES / "main.m3u8").read_text()
    media = {path.name: path.read_text() for path in SAMPLES.glob("main_*.m3u8")}
    encode = Encode(ladder=None, prefix="", multivariants={"hls": main}, media=media)  # type: ignore[arg-type]

    shared = encode_timeline.timeline(main, media.__getitem__)

    assert break_offsets(encode, []) == shared.boundaries_ms
    first_per_codec = ["main_avc-720p-a.m3u8", "main_hevc-1080p.m3u8", "main_av1-1080p.m3u8"]
    assert encode_timeline.timeline_uris(main)[:3] == first_per_codec
