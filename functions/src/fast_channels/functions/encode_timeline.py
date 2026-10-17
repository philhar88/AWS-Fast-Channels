"""The timeline of an HLS encode: its duration and the segment boundaries every variant shares.

One rule for the ingest pipeline (``ingest.finalize``, the encodes it verifies) and the agent's
``origins___register_vod_source`` (content encoded elsewhere), so both give the same break offsets for the same
encode. Channel Assembly can only insert an ad break where every track the player may select has a segment
boundary (channel-assembly.md §5.3). Audio is muxed into every MPEG-TS segment, so a variant is one track and its
boundaries are those of its media playlist; the first variant is the reference timeline.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from fast_channels.core.manifests import parse_multivariant, segment_boundaries_ms


@dataclass(frozen=True, slots=True)
class Timeline:
    """Duration of the reference variant's media playlist, and the interior boundaries every variant shares (ms)."""

    duration_ms: int
    boundaries_ms: list[int]


def timeline_uris(multivariant: str) -> list[str]:
    """The media playlist of every variant, reference (first) first, as written in ``multivariant``."""
    return list(dict.fromkeys(v.uri for v in parse_multivariant(multivariant)))


def duration_ms(media_playlist: str) -> int:
    """Exact duration of a media playlist (the Decimal sum of its EXTINF values) in milliseconds."""
    durations = (Decimal(line[8:].split(",", 1)[0]) for line in media_playlist.splitlines() if line[:8] == "#EXTINF:")
    return round(sum(durations, Decimal(0)) * 1000)


def timeline(multivariant: str, read: Callable[[str], str]) -> Timeline:
    """The timeline of the encode behind ``multivariant``; ``read(uri)`` returns a media playlist's text."""
    reference, *others = (read(uri) for uri in timeline_uris(multivariant))
    return Timeline(duration_ms=duration_ms(reference), boundaries_ms=segment_boundaries_ms(reference, *others))
