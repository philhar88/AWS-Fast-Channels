"""HLS readers and rewriters for MediaConvert, Channel Assembly and MediaTailor playlists.

Text in, text out: callers fetch and store objects. Rewrites keep every other byte of the playlist unchanged.
Every variant is AVC video with AAC audio muxed into the same MPEG-TS segments, so there are no audio or subtitle
renditions and no EXT-X-MAP.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import accumulate
from typing import TYPE_CHECKING, Final
from urllib.parse import urljoin

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

type Signature = tuple[tuple[str, ...], ...]

AUDIO_CODEC: Final = "mp4a.40.2"
"""RFC 6381 codec of the AAC-LC track muxed into every variant."""

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_VIDEO_FAMILIES: Final = {"avc1": "avc", "avc3": "avc", "hvc1": "hevc", "hev1": "hevc", "av01": "av1"}


def attributes(tag_line: str) -> dict[str, str]:
    """Attribute list of an ``#EXT-X-...:`` tag line, with quotes removed."""
    _, _, body = tag_line.partition(":")
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(body)}


@dataclass(frozen=True, slots=True)
class Variant:
    """One ``EXT-X-STREAM-INF`` entry of a multivariant playlist."""

    uri: str
    bandwidth: int
    average_bandwidth: int | None
    codecs: tuple[str, ...]
    resolution: tuple[int, int] | None
    frame_rate: float | None

    @property
    def video_codec(self) -> str | None:
        """Codec family (``avc``; ``hevc`` or ``av1`` would be foreign to these ladders) of the video in ``CODECS``."""
        return next((f for c in self.codecs if (f := _VIDEO_FAMILIES.get(c.split(".", 1)[0]))), None)

    @property
    def audio_codec(self) -> str | None:
        """The ``mp4a.*`` codec in ``CODECS``: the variant's muxed audio."""
        return next((c for c in self.codecs if c.startswith("mp4a")), None)


def _stream_infs(text: str) -> Iterator[tuple[int, str, str]]:
    """(line index, tag line, URI line) of every EXT-X-STREAM-INF."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            uri = next((u.strip() for u in lines[i + 1 :] if u.strip() and not u.startswith("#")), "")
            yield i, line, uri


def parse_multivariant(text: str) -> list[Variant]:
    """Variants of a multivariant playlist in order (I-frame and image streams excluded)."""
    variants = []
    for _, line, uri in _stream_infs(text):
        a = attributes(line)
        width, _, height = a.get("RESOLUTION", "").partition("x")
        variants.append(
            Variant(
                uri=uri,
                bandwidth=int(a["BANDWIDTH"]),
                average_bandwidth=int(a["AVERAGE-BANDWIDTH"]) if "AVERAGE-BANDWIDTH" in a else None,
                codecs=tuple(filter(None, a.get("CODECS", "").split(","))),
                resolution=(int(width), int(height)) if height else None,
                frame_rate=float(a["FRAME-RATE"]) if "FRAME-RATE" in a else None,
            )
        )
    return variants


def _signature_row(
    codec: str | None, size: tuple[int, int] | None, fps: float | None, audio: str | None
) -> tuple[str, ...]:
    return (codec or "", f"{size[0]}x{size[1]}" if size else "", f"{fps:.3f}" if fps else "", audio or "")


def ladder_signature(text: str) -> Signature:
    """Ordered (video codec family, WxH, frame rate to 3 decimals, audio codec) of every variant.

    Channel Assembly matches variants by position, so VOD sources in one source group must share this exactly.
    """
    return tuple(
        _signature_row(v.video_codec, v.resolution, v.frame_rate, v.audio_codec) for v in parse_multivariant(text)
    )


def expected_signature(ladder: Ladder) -> Signature:
    """The :func:`ladder_signature` that an encode of ``ladder`` must have."""
    return tuple(_signature_row("avc", (r.width, r.height), ladder.frame_rate.fps, AUDIO_CODEC) for r in ladder.video)


def media_segment_durations(text: str) -> list[float]:
    """``EXTINF`` durations of a media playlist, in order."""
    return [float(line[8:].split(",", 1)[0]) for line in text.splitlines() if line.startswith("#EXTINF:")]


def _boundaries(text: str) -> list[int]:
    durations = [Decimal(line[8:].split(",", 1)[0]) for line in text.splitlines() if line.startswith("#EXTINF:")]
    return [round(t * 1000) for t in accumulate(durations)]


def segment_boundaries_ms(*media_playlists: str) -> list[int]:
    """Interior segment boundaries (ms) that every media playlist shares.

    Pass the variants of one encode: their segments are cut on the same GOP grid, and with audio muxed into every
    segment a boundary of a variant is a boundary of all its tracks, so the result is what ad-break offsets use.
    """
    if not media_playlists:
        return []
    reference, *others = (_boundaries(text) for text in media_playlists)
    return [b for b in reference[:-1] if all(b in other for other in others)]


def _set_attribute(tag_line: str, name: str, value: int) -> str:
    pattern = re.compile(rf"(?<=[:,]){re.escape(name)}=[^,]*")
    if pattern.search(tag_line):
        return pattern.sub(f"{name}={value}", tag_line, count=1)
    return f"{tag_line},{name}={value}"


def normalize_bandwidth(text: str, nominal: Mapping[str, tuple[int, int]]) -> str:
    """Rewrite BANDWIDTH and AVERAGE-BANDWIDTH of every variant whose URI is in ``nominal`` (uri -> (peak, avg)).

    Channel Assembly copies the *current program's* BANDWIDTH into the channel multivariant, so a QVBR slate of a
    still image would advertise ~180 kbit/s for a 4 Mbit/s rung (e2e.md §3). Ladder-nominal values are stable.
    """
    lines = text.splitlines(keepends=True)
    for i, line, uri in _stream_infs(text):
        if uri in nominal:
            peak, average = nominal[uri]
            tag = _set_attribute(_set_attribute(line, "BANDWIDTH", peak), "AVERAGE-BANDWIDTH", average)
            lines[i] = tag + lines[i][len(line) :]
    return "".join(lines)


def nominal_bandwidths(ladder: Ladder, multivariant: str) -> dict[str, tuple[int, int]]:
    """Ladder-nominal (BANDWIDTH, AVERAGE-BANDWIDTH) per variant URI of a MediaConvert multivariant.

    Child playlists are ``<base>_<rendition name>.m3u8``; URIs that match no rendition are left out.
    """
    by_modifier = {r.name_modifier: r for r in ladder.video}
    nominal = {}
    for variant in parse_multivariant(multivariant):
        stem = variant.uri.rsplit("/", 1)[-1].removesuffix(".m3u8")
        if rendition := by_modifier.get(f"_{stem.rsplit('_', 1)[-1]}"):
            nominal[variant.uri] = ladder.nominal_bandwidth(rendition)
    return nominal


def absolutize_media_playlist(text: str, base_url: str) -> str:
    """Make segment URIs absolute against ``base_url`` (which should end with ``/``).

    MediaTailor re-hosts only the manifests of preconditioned SGAI ads, so their segment URIs must be absolute
    on our CDN (e2e.md §5.5); the multivariant's child URIs stay relative.
    """

    def rewrite(line: str) -> str:
        return urljoin(base_url, line.strip()) if line.strip() and not line.startswith("#") else line

    return "\n".join(rewrite(line) for line in text.splitlines()) + "\n"


def _variant_findings(text: str) -> Iterator[str]:
    # MediaTailor requires RESOLUTION, BANDWIDTH and CODECS on every variant (getting-started-ad-insertion).
    for _, line, uri in _stream_infs(text):
        a = attributes(line)
        if missing := [name for name in ("BANDWIDTH", "CODECS", "RESOLUTION") if name not in a]:
            yield f"{uri}: missing {', '.join(missing)}"
        if "FRAME-RATE" in a and not re.fullmatch(r"\d+\.\d{3}", a["FRAME-RATE"]):
            yield f"{uri}: FRAME-RATE should have 3 decimals"
        if "AUDIO" in a or "SUBTITLES" in a:
            yield f"{uri}: references a rendition group; audio is muxed and there are no subtitles"
        if not any(c.startswith("mp4a") for c in a.get("CODECS", "").split(",")):
            yield f"{uri}: CODECS lacks the muxed audio codec"
    if 0 < text.count("SCORE=") < sum(1 for _ in _stream_infs(text)):
        yield "SCORE must be on every variant or none"


def check_hls(multivariant: str) -> list[str]:
    """Findings against the multivariant checklist; an empty list means every check passed."""
    lines = multivariant.splitlines()
    if not lines or lines[0].strip() != "#EXTM3U":
        return ["playlist does not start with #EXTM3U"]
    if next(_stream_infs(multivariant), None) is None:  # not parse_multivariant: it needs BANDWIDTH on every variant
        return ["no EXT-X-STREAM-INF variants"]
    return list(_variant_findings(multivariant))
