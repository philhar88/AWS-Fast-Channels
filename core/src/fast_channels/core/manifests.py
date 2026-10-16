"""HLS readers and rewriters for MediaConvert, Channel Assembly and MediaTailor playlists.

Text in, text out: callers fetch and store objects. Rewrites keep every other byte of the playlist unchanged.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import accumulate
from typing import Final
from urllib.parse import urljoin
from xml.sax.saxutils import escape, unescape

from fast_channels.core.ladder import Ladder, VideoCodec
from fast_channels.core.profiles import CATALOGUE, RenditionProfile

type Signature = tuple[tuple[str, ...], ...]

BOUNDARY_TOLERANCE_MS: Final = 200
"""Audio segments end on AAC frame boundaries after the priming samples, so they trail the video boundary by
37-59 ms (AAC-LC), 73-101 ms (HE-AAC) and 115-148 ms (HE-AACv2) in the verified encodes (mediaconvert.md §8.3,
ladders-assets/manifests/full). Channel Assembly snaps breaks to the nearest common boundary."""

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_CODEC_FAMILY: Final = {
    "avc1": VideoCodec.AVC,
    "avc3": VideoCodec.AVC,
    "hvc1": VideoCodec.HEVC,
    "hev1": VideoCodec.HEVC,
    "av01": VideoCodec.AV1,
}


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
    audio_group: str | None

    @property
    def video_codec(self) -> str | None:
        """Codec family (``avc``, ``hevc``, ``av1``) of the first video codec in ``CODECS``."""
        for codec in self.codecs:
            if family := _CODEC_FAMILY.get(codec.split(".", 1)[0]):
                return family.value
        return None


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
                audio_group=a.get("AUDIO"),
            )
        )
    return variants


def _signature_row(
    codec: str | None, size: tuple[int, int] | None, fps: float | None, audio: str | None
) -> tuple[str, ...]:
    return (codec or "", f"{size[0]}x{size[1]}" if size else "", f"{fps:.3f}" if fps else "", audio or "")


def subtitle_renditions(text: str) -> list[dict[str, str]]:
    """Attributes of every ``EXT-X-MEDIA:TYPE=SUBTITLES`` of a multivariant playlist, in order."""
    media = (attributes(line) for line in text.splitlines() if line.startswith("#EXT-X-MEDIA:"))
    return [a for a in media if a.get("TYPE") == "SUBTITLES"]


def _subtitle_row(language: str, name: str) -> tuple[str, ...]:
    return (SUBTITLES_ROW, language, name, "")


SUBTITLES_ROW: Final = "subtitles"
"""First field of a signature row that stands for a subtitle rendition instead of a variant."""


def ladder_signature(text: str) -> Signature:
    """Ordered (codec family, WxH, frame rate to 3 decimals, audio group) of every variant, then
    (``subtitles``, LANGUAGE, NAME, ``""``) of every subtitle rendition.

    Channel Assembly matches variants and subtitle renditions by position, so VOD sources in one source group must
    share this exactly; a difference in subtitles drops ad breaks silently (docs/captions.md).
    """
    variants = (
        _signature_row(v.video_codec, v.resolution, v.frame_rate, v.audio_group) for v in parse_multivariant(text)
    )
    subtitles = (_subtitle_row(a.get("LANGUAGE", ""), a.get("NAME", "")) for a in subtitle_renditions(text))
    return (*variants, *subtitles)


def expected_signature(ladder: Ladder, profile: RenditionProfile = CATALOGUE[0]) -> Signature:
    """The :func:`ladder_signature` that ``ladder``'s manifest for ``profile`` (default: ``hls``) must have."""
    variants = (
        _signature_row(r.codec.value, (r.width, r.height), ladder.frame_rate.fps, r.audio_group)
        for r in ladder.video
        if profile.selects(r)
    )
    return (*variants, *(_subtitle_row(c.language, c.label) for c in ladder.captions))


def media_segment_durations(text: str) -> list[float]:
    """``EXTINF`` durations of a media playlist, in order."""
    return [float(line[8:].split(",", 1)[0]) for line in text.splitlines() if line.startswith("#EXTINF:")]


def _boundaries(text: str) -> list[int]:
    durations = [Decimal(line[8:].split(",", 1)[0]) for line in text.splitlines() if line.startswith("#EXTINF:")]
    return [round(t * 1000) for t in accumulate(durations)]


def segment_boundaries_ms(*media_playlists: str, tolerance_ms: int = BOUNDARY_TOLERANCE_MS) -> list[int]:
    """Interior segment boundaries of the first playlist that every other playlist shares within tolerance.

    Pass a video playlist first: the result is in its timeline and is what ad-break offsets must use.
    """
    if not media_playlists:
        return []
    reference, *others = (_boundaries(text) for text in media_playlists)
    interior = reference[:-1]
    return [b for b in interior if all(any(abs(b - o) <= tolerance_ms for o in other) for other in others)]


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


def bump_iframe_version(text: str) -> str:
    """Raise an I-frame playlist with ``EXT-X-MAP`` to ``EXT-X-VERSION:5`` (MediaConvert writes 4; spec §8)."""
    if "#EXT-X-I-FRAMES-ONLY" not in text or "#EXT-X-MAP" not in text:
        return text
    return re.sub(r"^#EXT-X-VERSION:[0-4]\s*$", "#EXT-X-VERSION:5", text, count=1, flags=re.MULTILINE)


def absolutize_media_playlist(text: str, base_url: str) -> str:
    """Make segment and ``EXT-X-MAP`` URIs absolute against ``base_url`` (which should end with ``/``).

    MediaTailor re-hosts only the manifests of preconditioned SGAI ads, so their segment URIs must be absolute
    on our CDN (e2e.md §5.5); the multivariant's child URIs stay relative.
    """

    def rewrite(line: str) -> str:
        if line.startswith("#EXT-X-MAP:"):
            return re.sub(r'URI="([^"]+)"', lambda m: f'URI="{urljoin(base_url, m.group(1))}"', line)
        if line.strip() and not line.startswith("#"):
            return urljoin(base_url, line.strip())
        return line

    return "\n".join(rewrite(line) for line in text.splitlines()) + "\n"


def _media_findings(text: str) -> Iterator[str]:
    groups: dict[str, list[dict[str, str]]] = {}
    for line in text.splitlines():
        if line.startswith("#EXT-X-MEDIA:") and (a := attributes(line)).get("TYPE") == "AUDIO":
            groups.setdefault(a.get("GROUP-ID", ""), []).append(a)
    for group, renditions in groups.items():
        for a in renditions:
            language = a.get("LANGUAGE", "")
            if not language or len(language.split("-")[0]) != 2:
                yield f"audio group {group}: LANGUAGE {language!r} should be a 2-letter BCP-47 code"
            if not a.get("NAME") or "CHANNELS" not in a:
                yield f"audio group {group}: NAME and CHANNELS are required"
            if a.get("DEFAULT") == "YES" and a.get("AUTOSELECT") != "YES":
                yield f"audio group {group}: DEFAULT=YES needs AUTOSELECT=YES"
        if sum(a.get("DEFAULT") == "YES" for a in renditions) != 1:
            yield f"audio group {group}: exactly one rendition must be DEFAULT=YES"


def _subtitle_findings(text: str) -> Iterator[str]:
    renditions = subtitle_renditions(text)
    groups = {a.get("GROUP-ID", "") for a in renditions}
    for a in renditions:
        name = a.get("NAME", "")
        if not name or not a.get("URI"):
            yield f"subtitles {a.get('GROUP-ID', '')}: NAME and URI are required"
        if len(a.get("LANGUAGE", "").split("-")[0]) != 2:
            yield f"subtitles {name}: LANGUAGE {a.get('LANGUAGE', '')!r} should be a 2-letter BCP-47 code"
        if sum(b.get("NAME") == name and b.get("GROUP-ID") == a.get("GROUP-ID") for b in renditions) > 1:
            yield f"subtitles {name}: NAME must be unique in group {a.get('GROUP-ID', '')}"
    for _, line, uri in _stream_infs(text):
        group = attributes(line).get("SUBTITLES")
        if group is not None and group not in groups:
            yield f"{uri}: SUBTITLES group {group!r} has no rendition"


def _variant_findings(text: str, variants: list[Variant]) -> Iterator[str]:
    required = ("BANDWIDTH", "AVERAGE-BANDWIDTH", "CODECS", "RESOLUTION", "FRAME-RATE", "VIDEO-RANGE")
    for _, line, uri in _stream_infs(text):
        a = attributes(line)
        if missing := [name for name in required if name not in a]:
            yield f"{uri}: missing {', '.join(missing)}"
        if "FRAME-RATE" in a and not re.fullmatch(r"\d+\.\d{3}", a["FRAME-RATE"]):
            yield f"{uri}: FRAME-RATE should have 3 decimals"
        if "AUDIO" in a and not any(c.startswith("mp4a") for c in a.get("CODECS", "").split(",")):
            yield f"{uri}: CODECS lacks the audio codec of group {a['AUDIO']}"
    first = variants[0]
    if first.video_codec != VideoCodec.AVC or not first.resolution or first.resolution[1] != 720:
        yield "first variant should be the ~2 Mbit/s AVC 720p rung (Apple 1.32)"
    if min(v.bandwidth for v in variants) > 192_000:
        yield "no variant has BANDWIDTH <= 192 kbit/s (iOS cellular)"
    for family in sorted({v.video_codec or "" for v in variants}):
        if min(v.bandwidth for v in variants if (v.video_codec or "") == family) > 500_000:
            yield f"lowest {family} variant is above 500 kbit/s (hls.js start rule)"
    top = max((v.resolution for v in variants if v.resolution), key=lambda r: r[0] * r[1], default=None)
    if top and sum(v.resolution == top for v in variants) < 2:
        yield "fewer than 2 variants at the top resolution (Apple 9.10)"
    if 0 < text.count("SCORE=") < len(variants):
        yield "SCORE must be on every variant or none"


def _iframe_findings(text: str, variants: list[Variant]) -> Iterator[str]:
    iframes = [attributes(line) for line in text.splitlines() if line.startswith("#EXT-X-I-FRAME-STREAM-INF:")]
    have = {a.get("RESOLUTION") for a in iframes}
    sizes = {f"{v.resolution[0]}x{v.resolution[1]}" for v in variants if v.resolution}
    for size in sorted(sizes - have):
        yield f"no I-frame playlist for {size} (Apple 6.1)"
    if any("CODECS" not in a or "RESOLUTION" not in a for a in iframes):
        yield "I-frame variants need CODECS and RESOLUTION"


def check_hls(multivariant: str) -> list[str]:
    """Findings against the ladders.md §8 multivariant checklist; an empty list means every check passed."""
    lines = multivariant.splitlines()
    if not lines or lines[0].strip() != "#EXTM3U":
        return ["playlist does not start with #EXTM3U"]
    findings = []
    if multivariant.count("#EXT-X-INDEPENDENT-SEGMENTS") != 1:
        findings.append("EXT-X-INDEPENDENT-SEGMENTS must appear exactly once")
    variants = parse_multivariant(multivariant)
    if not variants:
        return [*findings, "no EXT-X-STREAM-INF variants"]
    return [
        *findings,
        *_variant_findings(multivariant, variants),
        *_iframe_findings(multivariant, variants),
        *_media_findings(multivariant),
        *_subtitle_findings(multivariant),
    ]


_SEGMENT_TEMPLATE: Final = re.compile(r"<SegmentTemplate\b[^>]*>")
_TEMPLATE_URL: Final = re.compile(r'\b(initialization|media)="([^"]+)"')


def absolutize_mpd(mpd: str, base_url: str) -> str:
    """Make every ``SegmentTemplate`` ``initialization`` and ``media`` URL absolute against ``base_url``.

    MediaTailor resolves the segments of a preconditioned DASH ad against the location of the MPD it fetched and
    ignores ``BaseURL`` elements (verified at MPD and Period level, docs/captions.md), so an ad MPD
    stored apart from its segments needs absolute templates. ``$Number%09d$`` identifiers are kept.
    """

    def absolute(attribute: re.Match[str]) -> str:
        name, url = attribute.groups()
        return f'{name}="{escape(urljoin(base_url, unescape(url)))}"'

    return _SEGMENT_TEMPLATE.sub(lambda tag: _TEMPLATE_URL.sub(absolute, tag.group(0)), mpd)
