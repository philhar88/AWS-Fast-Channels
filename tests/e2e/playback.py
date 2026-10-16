"""What a viewer gets: readers for the HLS, DASH, HLS-Interstitials and VAST documents the stage serves.

Pure functions over text (unit-tested against wire samples in ``data/``). They classify, they do not fetch:
:mod:`harness` does the HTTP. URL shapes are the verified ones of e2e.md §4-5: SSAI stitches preconditioned ad
segments as MediaTailor beacon redirects (``…/v1/segment/…``) behind an ``EXT-X-MAP`` on the CDN's ``/ads/`` path,
SGAI decorates the break's DATERANGE with ``CLASS="com.apple.hls.interstitial"`` and an asset list, and DASH ad
periods take their segments from ``…/v1/dashsegment/…``.
"""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urljoin

from fast_channels.core.manifests import attributes, parse_multivariant

INTERSTITIAL_CLASS: Final = "com.apple.hls.interstitial"
AD_SEGMENT_MARKERS: Final = ("/segment/", "/tm/", "/ads/", "/preconditioned-ads/")
"""URI parts of stitched ad segments: beacon redirects, transcoded ads, our preconditioned creatives."""
DASH_AD_MARKER: Final = "/dashsegment/"
MPD: Final = "{urn:mpeg:dash:schema:mpd:2011}"
VAST: Final = "{http://www.iab.com/VAST}"
_VARIABLE = re.compile(r"\{\$([A-Za-z0-9_-]+)\}")


@dataclass(frozen=True, slots=True)
class Segment:
    """One media segment of an HLS media playlist."""

    uri: str
    duration: float
    starts: datetime | None
    """Program date-time of its first frame (``EXT-X-PROGRAM-DATE-TIME``, carried forward by durations)."""
    map_uri: str | None

    @property
    def is_ad(self) -> bool:
        """Stitched ad content, as opposed to programs and slates (both served from the CDN's ``/assets/``)."""
        return any(marker in uri for uri in (self.uri, self.map_uri or "") for marker in AD_SEGMENT_MARKERS)

    @property
    def ends(self) -> datetime | None:
        return None if self.starts is None else self.starts + timedelta(seconds=self.duration)


@dataclass(frozen=True, slots=True)
class Window:
    """When something airs (a scheduled break), with a tolerance for Channel Assembly's filler-boundary snapping."""

    starts: datetime
    ends: datetime

    def holds(self, moment: datetime, *, slack: timedelta) -> bool:
        return self.starts - slack <= moment < self.ends + slack


@dataclass(frozen=True, slots=True)
class Mpd:
    """The parts of a DASH MPD the suite checks."""

    type: str
    periods: tuple[str, ...]
    ad_periods: tuple[str, ...]
    codecs: frozenset[str]


@dataclass(frozen=True, slots=True)
class MediaFile:
    """A streaming VAST MediaFile."""

    mime_type: str
    uri: str


def renditions(multivariant: str, base_url: str) -> dict[str, str]:
    """Absolute media playlist URL per codec family (its first variant: ``avc``, ``hevc``, ``av1``) and per audio
    group (``audio:<GROUP-ID>``), selected by attributes: SSAI names the children 0.m3u8, 1.m3u8, ... (e2e.md §8)."""
    picked: dict[str, str] = {}
    for variant in parse_multivariant(multivariant):
        if variant.video_codec:
            picked.setdefault(variant.video_codec, urljoin(base_url, variant.uri))
    for line in multivariant.splitlines():
        if line.startswith("#EXT-X-MEDIA:") and (media := attributes(line)).get("TYPE") == "AUDIO" and "URI" in media:
            picked.setdefault(f"audio:{media.get('GROUP-ID', '')}", urljoin(base_url, media["URI"]))
    return picked


def video_codecs(multivariant: str) -> set[str]:
    """Codec families of the variants (``avc``, ``hevc``, ``av1``)."""
    return {codec for variant in parse_multivariant(multivariant) if (codec := variant.video_codec)}


def codec_tags(multivariant: str) -> set[str]:
    """Video sample-entry codes in ``CODECS`` (``avc1``, ``hvc1``, ``av01``)."""
    tags = {codec.partition(".")[0] for variant in parse_multivariant(multivariant) for codec in variant.codecs}
    return {tag for tag in tags if not tag.startswith("mp4a")}


def media_segments(text: str, base_url: str) -> list[Segment]:
    """The segments of a media playlist in order, URIs absolute."""
    segments: list[Segment] = []
    moment: datetime | None = None
    duration: float | None = None
    map_uri: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            moment = datetime.fromisoformat(line.partition(":")[2])
        elif line.startswith("#EXT-X-MAP:"):
            map_uri = urljoin(base_url, attributes(line)["URI"])
        elif line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            segments.append(Segment(urljoin(base_url, line), duration, moment, map_uri))
            moment = None if moment is None else moment + timedelta(seconds=duration)
            duration = None
    return segments


def ad_seconds(segments: Iterable[Segment], window: Window, *, slack: timedelta = timedelta(seconds=15)) -> float:
    """Seconds of ad segments that start within ``window``."""
    return sum(s.duration for s in segments if s.is_ad and s.starts is not None and window.holds(s.starts, slack=slack))


def break_windows(programs: Iterable[Mapping[str, Any]]) -> list[Window]:
    """Air times of the breaks of published programs (EPG entries: ``start``, and each break's ``offset_ms`` into
    the content and ``duration_ms``); a program's earlier breaks push its later ones back by their length."""
    windows: list[Window] = []
    for program in programs:
        start = datetime.fromisoformat(program["start"])
        inserted = 0
        for brk in program.get("breaks", []):
            begins = start + timedelta(milliseconds=brk["offset_ms"] + inserted)
            windows.append(Window(begins, begins + timedelta(milliseconds=brk["duration_ms"])))
            inserted += brk["duration_ms"]
    return sorted(windows, key=lambda window: window.starts)


def defines(multivariant: str) -> dict[str, str]:
    """``EXT-X-DEFINE`` variables (``awsSessionData``, ``awsSessionId``, ...) of a playlist."""
    values = (attributes(line) for line in multivariant.splitlines() if line.startswith("#EXT-X-DEFINE:"))
    return {value["NAME"]: value.get("VALUE", "") for value in values if "NAME" in value}


def substitute(text: str, variables: Mapping[str, str]) -> str:
    """Replace ``{$name}`` references with their values (unknown names stay)."""
    return _VARIABLE.sub(lambda m: variables.get(m[1], m[0]), text)


def interstitials(text: str) -> dict[str, dict[str, str]]:
    """HLS Interstitial DATERANGEs of a media playlist by ID (``X-ASSET-LIST``, ``START-DATE``, ``DURATION``...)."""
    ranges = (attributes(line) for line in text.splitlines() if line.startswith("#EXT-X-DATERANGE:"))
    return {r["ID"]: r for r in ranges if r.get("CLASS") == INTERSTITIAL_CLASS and "ID" in r}


def asset_list(text: str) -> list[dict[str, Any]]:
    """``ASSETS`` of an HLS Interstitials asset list (each with ``URI`` and ``DURATION``)."""
    document = json.loads(text)
    if not isinstance(document, dict) or not isinstance(assets := document.get("ASSETS"), list):
        raise ValueError(f"not an asset list: {text[:200]}")
    return assets


def parse_mpd(text: str) -> Mpd:
    """Type, period ids, ad period ids (segments from MediaTailor's ``dashsegment`` path) and codecs of an MPD."""
    root = ET.fromstring(text)
    periods = root.findall(f"{MPD}Period")
    ads = [p for p in periods if any(DASH_AD_MARKER in (url.text or "") for url in p.iter(f"{MPD}BaseURL"))]
    codecs = {r.get("codecs", "") for r in root.iter(f"{MPD}Representation")} - {""}
    return Mpd(root.get("type", ""), _ids(periods), _ids(ads), frozenset(codecs))


def _ids(periods: Iterable[ET.Element]) -> tuple[str, ...]:
    return tuple(period.get("id", "") for period in periods)


def vast_media_files(text: str) -> list[MediaFile]:
    """Every MediaFile of a VAST document."""
    root = ET.fromstring(text)
    return [
        MediaFile(element.get("type", ""), (element.text or "").strip()) for element in root.iter(f"{VAST}MediaFile")
    ]


def vast_ads(text: str) -> list[str]:
    """Creative ids of a VAST pod, in sequence order."""
    root = ET.fromstring(text)
    return [element.get("id", "") for element in root.iter(f"{VAST}Creative")]
