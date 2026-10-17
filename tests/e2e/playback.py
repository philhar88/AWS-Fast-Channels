"""What a viewer gets: readers for the HLS and VAST documents the stage serves.

Pure functions over text (unit-tested against samples in ``data/``). They classify, they do not fetch:
:mod:`harness` does the HTTP. URL shapes are the verified ones of e2e.md §4-5: SSAI stitches ad segments as
MediaTailor beacon redirects (``…/v1/segment/…``), and every break starts at the ``EXT-X-DATERANGE`` (SCTE-35 out)
that Channel Assembly writes. Every variant is MPEG-TS with its AAC audio muxed, so there are no audio or subtitle
renditions to follow and no ``EXT-X-MAP``.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from fast_channels.core.manifests import attributes, parse_multivariant

AD_SEGMENT_MARKERS: Final = ("/segment/", "/tm/", "/ads/")
"""URI parts of stitched ad segments: beacon redirects, transcoded ads and our preconditioned creatives."""
_ASSET_PATH = re.compile(r"/assets/([a-z0-9][a-z0-9-]*)/")
VAST: Final = "{http://www.iab.com/VAST}"


@dataclass(frozen=True, slots=True)
class Segment:
    """One media segment of an HLS media playlist."""

    uri: str
    duration: float
    starts: datetime | None
    """Program date-time of its first frame (``EXT-X-PROGRAM-DATE-TIME``, carried forward by durations)."""

    @property
    def name(self) -> str:
        """The last part of its URI path (``main_avc-720p_00006.ts``): the same in the encode and on the air."""
        return PurePosixPath(urlsplit(self.uri).path).name

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
class Span:
    """A segment of a VOD media playlist on its content timeline, in ms: cumulative EXTINFs rounded to the ms, as
    ``core.manifests.segment_boundaries_ms`` (and so the break offsets) count them."""

    uri: str
    starts_ms: int
    ends_ms: int

    @property
    def name(self) -> str:
        """The last part of its URI path, as :attr:`Segment.name`."""
        return PurePosixPath(urlsplit(self.uri).path).name

    @property
    def duration_ms(self) -> int:
        return self.ends_ms - self.starts_ms


@dataclass(frozen=True, slots=True)
class Splice:
    """Where a live media playlist leaves for a break: Channel Assembly's ad marker (an ``EXT-X-DATERANGE`` with
    ``SCTE35-OUT``) and the segments on either side of it."""

    id: str
    starts: datetime
    """The marker's ``START-DATE``."""
    before: Segment | None
    """The last segment before the marker (None when the playlist starts at it)."""
    after: Segment | None
    """The first segment of the break (None when the playlist ends at the marker)."""

    @property
    def complete(self) -> bool:
        return self.before is not None and self.after is not None


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
    return [segment for segment, _ in _segments_and_markers(text, base_url) if segment is not None]


def splices(text: str, base_url: str) -> list[Splice]:
    """The ad markers of a live media playlist (``EXT-X-DATERANGE`` with ``SCTE35-OUT``) in order, each with the
    segments around it."""
    found: list[Splice] = []
    previous: Segment | None = None
    for segment, markers in _segments_and_markers(text, base_url):
        found += [Splice(m["ID"], datetime.fromisoformat(m["START-DATE"]), previous, segment) for m in markers]
        previous = segment
    return found


def _segments_and_markers(text: str, base_url: str) -> Iterator[tuple[Segment | None, list[dict[str, str]]]]:
    """Each segment with the SCTE-35 out markers just before it, then ``(None, markers)`` for markers after the
    last segment."""
    moment: datetime | None = None
    duration: float | None = None
    markers: list[dict[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            moment = datetime.fromisoformat(line.partition(":")[2])
        elif line.startswith("#EXT-X-DATERANGE:"):
            marker = attributes(line)
            if "SCTE35-OUT" in marker and {"ID", "START-DATE"} <= marker.keys():
                markers.append(marker)
        elif line.startswith("#EXTINF:"):
            duration = float(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            yield Segment(urljoin(base_url, line), duration, moment), markers
            moment = None if moment is None else moment + timedelta(seconds=duration)
            duration, markers = None, []
    if markers:
        yield None, markers


def spans(text: str) -> list[Span]:
    """The segments of a VOD media playlist on its content timeline (URIs as written)."""
    found: list[Span] = []
    elapsed = Decimal(0)
    starts = 0
    duration: Decimal | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            duration = Decimal(line[8:].split(",", 1)[0])
        elif line and not line.startswith("#") and duration is not None:
            elapsed += duration
            ends = round(elapsed * 1000)
            found.append(Span(line, starts, ends))
            starts, duration = ends, None
    return found


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
