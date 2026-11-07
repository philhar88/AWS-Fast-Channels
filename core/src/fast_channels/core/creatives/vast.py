"""The progressive file of a creative registered by its VAST document.

The readiness function conditions a creative from its mezzanine, the best progressive MP4 of its inline linear ad:
the largest frame no taller than 1080 lines, then the highest bitrate. A Wrapper is not followed (register the inline
ad it leads to), and a streaming file is not used: the pipeline's own encode is what conforms to the ladder.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final
from xml.etree import ElementTree as ET

MAX_DOCUMENT: Final = 1_000_000
"""The largest VAST document read (bytes)."""
MAX_HEIGHT: Final = 1080


class VastError(ValueError):
    """The document is not a usable VAST ad."""


@dataclass(frozen=True, slots=True)
class MediaFile:
    """A progressive MP4 of the ad."""

    url: str
    width: int
    height: int
    bitrate: int


def _local(tag: str) -> str:
    """A tag without its namespace (VAST 4 documents may have one, VAST 3 ones have none)."""
    return tag.rpartition("}")[2]


def _children(element: ET.Element, name: str) -> Iterator[ET.Element]:
    return (child for child in element if _local(child.tag) == name)


def _int(value: str | None) -> int:
    try:
        return int(float(value or 0))
    except ValueError:
        return 0


def progressive_files(document: str) -> list[MediaFile]:
    """Every progressive MP4 of the document's inline linear ads, best first (module docstring).

    Raises:
        VastError: too large, with a DOCTYPE (entities are not read), not XML, not VAST, or a Wrapper.
    """
    if len(document.encode()) > MAX_DOCUMENT:
        raise VastError(f"the VAST document is over {MAX_DOCUMENT} bytes")
    if "<!DOCTYPE" in document.upper():
        raise VastError("the VAST document has a DOCTYPE")
    try:
        root = ET.fromstring(document)  # noqa: S314 - no DOCTYPE (so no entities), and size-capped above
    except ET.ParseError as error:
        raise VastError(f"not XML: {error}") from None
    if _local(root.tag) != "VAST":
        raise VastError(f"not a VAST document (<{_local(root.tag)}>)")
    ads = list(_children(root, "Ad"))
    if any(next(_children(ad, "Wrapper"), None) is not None for ad in ads):
        raise VastError("a Wrapper is not followed: register the inline ad it leads to")
    files = [
        MediaFile(
            url=(media.text or "").strip(),
            width=_int(media.get("width")),
            height=_int(media.get("height")),
            bitrate=_int(media.get("bitrate") or media.get("maxBitrate")),
        )
        for ad in ads
        for inline in _children(ad, "InLine")
        for creatives in _children(inline, "Creatives")
        for creative in _children(creatives, "Creative")
        for linear in _children(creative, "Linear")
        for files in _children(linear, "MediaFiles")
        for media in _children(files, "MediaFile")
        if media.get("delivery", "").lower() == "progressive"
        and media.get("type", "").lower() == "video/mp4"
        and (media.text or "").strip().startswith("https://")
    ]
    usable = [f for f in files if f.height <= MAX_HEIGHT]
    return sorted(usable, key=lambda f: (f.width * f.height, f.bitrate), reverse=True)


def mezzanine(document: str) -> MediaFile:
    """The document's best progressive MP4 (:func:`progressive_files`).

    Raises:
        VastError: the document is not usable, or it has no progressive https MP4 of at most 1080 lines.
    """
    if not (files := progressive_files(document)):
        raise VastError("no progressive https MP4 of at most 1080 lines")
    return files[0]
