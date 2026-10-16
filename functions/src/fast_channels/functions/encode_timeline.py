"""The timeline of an HLS encode: its duration and the segment boundaries every track shares.

One rule for the ingest pipeline (``ingest.finalize``, the encodes it verifies) and the agent's
``origins___register_vod_source`` (content encoded elsewhere), so both give the same break offsets for the same
encode. Channel Assembly can only insert an ad break where every track the player may select has a segment
boundary (channel-assembly.md §5.3):

* renditions of one video codec share their GOP and segment grid, so the first variant of each codec stands for
  its codec, and the first variant overall is the reference timeline (offsets are in video time);
* audio segments end on codec frame boundaries that differ by codec and profile, so every audio rendition counts;
* IMSC subtitle segments follow the video grid, but they are tracks too, so every subtitle rendition counts.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from fast_channels.core.manifests import attributes, parse_multivariant, segment_boundaries_ms


@dataclass(frozen=True, slots=True)
class Timeline:
    """Duration of the reference video playlist, and the interior boundaries every track shares (ms)."""

    duration_ms: int
    boundaries_ms: list[int]


def timeline_uris(multivariant: str) -> list[str]:
    """The media playlists that stand for every track, reference first: the first variant, the first variant of
    each other video codec, then every audio and subtitle rendition (URIs as written in ``multivariant``)."""
    by_codec: dict[str | None, str] = {}
    for variant in parse_multivariant(multivariant):
        by_codec.setdefault(variant.video_codec, variant.uri)
    media = [attributes(line) for line in multivariant.splitlines() if line.startswith("#EXT-X-MEDIA:")]
    renditions = [a["URI"] for kind in ("AUDIO", "SUBTITLES") for a in media if a.get("TYPE") == kind and "URI" in a]
    return list(dict.fromkeys([*by_codec.values(), *renditions]))


def duration_ms(media_playlist: str) -> int:
    """Exact duration of a media playlist (the Decimal sum of its EXTINF values) in milliseconds."""
    durations = (Decimal(line[8:].split(",", 1)[0]) for line in media_playlist.splitlines() if line[:8] == "#EXTINF:")
    return round(sum(durations, Decimal(0)) * 1000)


def timeline(multivariant: str, read: Callable[[str], str]) -> Timeline:
    """The timeline of the encode behind ``multivariant``; ``read(uri)`` returns a media playlist's text."""
    reference, *others = (read(uri) for uri in timeline_uris(multivariant))
    return Timeline(duration_ms=duration_ms(reference), boundaries_ms=segment_boundaries_ms(reference, *others))
