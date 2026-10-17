"""What a viewer gets: readers for the HLS, HLS-Interstitials and VAST documents the stage serves.

Pure functions over text (unit-tested against samples in ``data/``). They classify, they do not fetch:
:mod:`harness` does the HTTP. URL shapes are the verified ones of e2e.md §4-5: SSAI stitches ad segments as
MediaTailor beacon redirects (``…/v1/segment/…``), and SGAI decorates the break's DATERANGE with
``CLASS="com.apple.hls.interstitial"`` and an asset list. Every variant is MPEG-TS with its AAC audio muxed, so
there are no audio or subtitle renditions to follow and no ``EXT-X-MAP``.
"""

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final
from urllib.parse import urljoin

from fast_channels.core.manifests import attributes, parse_multivariant

INTERSTITIAL_CLASS: Final = "com.apple.hls.interstitial"
AD_SEGMENT_MARKERS: Final = ("/segment/", "/tm/", "/ads/", "/preconditioned-ads/")
"""URI parts of stitched ad segments: beacon redirects, transcoded ads and our preconditioned creatives."""
_ASSET_PATH = re.compile(r"/assets/([a-z0-9][a-z0-9-]*)/")
VAST: Final = "{http://www.iab.com/VAST}"
_VARIABLE = re.compile(r"\{\$([A-Za-z0-9_-]+)\}")


@dataclass(frozen=True, slots=True)
class Segment:
    """One media segment of an HLS media playlist."""

    uri: str
    duration: float
    starts: datetime | None
    """Program date-time of its first frame (``EXT-X-PROGRAM-DATE-TIME``, carried forward by durations)."""

    @property
    def is_ad(self) -> bool:
        """Stitched ad content recognisable from its URI alone (beacon redirects, transcoded or re-hosted ads)."""
        return any(marker in self.uri for marker in AD_SEGMENT_MARKERS)

    @property
    def asset_id(self) -> str | None:
        """The media-library asset whose encode this segment is (``/assets/<asset id>/…`` on the CDN)."""
        match = _ASSET_PATH.search(self.uri)
        return match.group(1) if match else None

    def is_ad_of(self, house_ads: Collection[str]) -> bool:
        """An ad: recognisable from its URI, or a segment of one of the kit's preconditioned house-ad assets.

        SSAI can serve a preconditioned ad's segments straight from the creative's encode on the CDN, without a
        beacon redirect (it did for CMAF ad audio, 2026-10-16); then only the asset id identifies them.
        """
        return self.is_ad or self.asset_id in house_ads

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
class MediaFile:
    """A streaming VAST MediaFile."""

    mime_type: str
    uri: str


def renditions(multivariant: str, base_url: str) -> dict[str, str]:
    """Absolute media playlist URL of every variant, in order, keyed by its RESOLUTION (``1280x720``, with
    ``@<BANDWIDTH>`` added when two variants share one). SSAI names the children 0.m3u8, 1.m3u8, ... (e2e.md §8)."""
    picked: dict[str, str] = {}
    for variant in parse_multivariant(multivariant):
        size = f"{variant.resolution[0]}x{variant.resolution[1]}" if variant.resolution else variant.uri
        key = f"{size}@{variant.bandwidth}" if size in picked else size
        picked.setdefault(key, urljoin(base_url, variant.uri))
    return picked


def media_segments(text: str, base_url: str) -> list[Segment]:
    """The segments of a media playlist in order, URIs absolute."""
    segments: list[Segment] = []
    moment: datetime | None = None
    duration: float | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            moment = datetime.fromisoformat(line.partition(":")[2])
        elif line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            segments.append(Segment(urljoin(base_url, line), duration, moment))
            moment = None if moment is None else moment + timedelta(seconds=duration)
            duration = None
    return segments


def ad_seconds(
    segments: Iterable[Segment],
    window: Window,
    *,
    house_ads: Collection[str] = (),
    slack: timedelta = timedelta(seconds=15),
) -> float:
    """Seconds of ad segments (see :meth:`Segment.is_ad_of`) that start within ``window``."""
    return sum(
        s.duration
        for s in segments
        if s.is_ad_of(house_ads) and s.starts is not None and window.holds(s.starts, slack=slack)
    )


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


def vast_durations(text: str) -> list[float]:
    """Linear durations of a VAST pod's ads in seconds, in sequence order."""
    root = ET.fromstring(text)
    hours_minutes_seconds = (d.text.split(":") for d in root.iter(f"{VAST}Duration") if d.text)
    return [int(h) * 3600 + int(m) * 60 + float(s) for h, m, s in hours_minutes_seconds]
