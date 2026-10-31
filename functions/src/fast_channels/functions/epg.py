"""Electronic programme guide feeds: ``s3://<media>/epg/<channel>.json`` (CONTRACTS §I) and the XMLTV twin ``.xml``.

The MediaLibrary ``PROGRAM`` items are the source of truth for a channel's schedule: Channel Assembly deletes aired
programs about 15 minutes after they finish (channel-assembly.md §5.6). The feeds are rendered from those items after
every schedule change and served publicly through the CDN's default (S3) behaviour. ``SKIPPED`` programs (removed
unaired because the channel started after their start) stay in the JSON feed with their status and are left out of
the XMLTV feed, which lists what airs.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Final, Literal
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import BaseModel, Field

from fast_channels.functions.aws import client

EPG_PREFIX: Final = "epg/"
CACHE_CONTROL: Final = "public, max-age=30"
"""Short enough that a published change reaches viewers' guides within half a minute through the CDN."""
GENERATOR: Final = "FAST Channels on AWS"

type ProgramStatus = Literal["PLANNED", "PUBLISHED", "AIRED", "SKIPPED"]


class EpgBreak(BaseModel):
    """An ad break inside a program: ``offset_ms`` into the content, airing for ``duration_ms``. With the ops module
    each break also names its SCTE-35 segmentation event id and UPID, as in the HLS ``DATERANGE`` and the ad request,
    so guides and buyers can join a break without recomputing them; without it the feed is v3.0's."""

    offset_ms: int
    duration_ms: int
    event_id: int | None = Field(
        default=None, exclude_if=lambda value: value is None, description="SCTE-35 segmentation event id (ops module)."
    )
    upid: str | None = Field(
        default=None, exclude_if=lambda value: value is None, description="SCTE-35 segmentation UPID (ops module)."
    )


class EpgProgram(BaseModel):
    """One guide entry. ``start``/``end`` are on-air times (content plus ad breaks)."""

    name: str = Field(description="Channel Assembly program name.")
    title: str
    content_id: str | None
    asset_id: str
    start: str = Field(description="UTC ISO-8601 start.")
    end: str = Field(description="UTC ISO-8601 end.")
    rating: str | None
    genres: list[str]
    breaks: list[EpgBreak]
    status: ProgramStatus = Field(
        description="PUBLISHED (scheduled), AIRED (Channel Assembly started it) or SKIPPED (removed unaired: the "
        "channel started after the program's start)."
    )


class Epg(BaseModel):
    """A channel's guide feed, programs in air order."""

    channel: str
    generated_at: str
    ladder_version: str
    programs: list[EpgProgram]


def feed(*, channel: str, ladder_version: str, programs: Sequence[EpgProgram], generated_at: str) -> Epg:
    """The guide of ``channel`` with ``programs`` in air order."""
    ordered = sorted(programs, key=lambda p: (datetime.fromisoformat(p.start), p.name))
    return Epg(channel=channel, generated_at=generated_at, ladder_version=ladder_version, programs=ordered)


def _xmltv_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%Y%m%d%H%M%S +0000")


def xmltv(epg: Epg) -> str:
    """The guide as an XMLTV document (one ``channel``; ``programme`` elements in DTD child order); programs that do
    not air (``SKIPPED``) are left out."""
    tv = Element("tv", {"generator-info-name": GENERATOR, "date": _xmltv_time(epg.generated_at)})
    SubElement(SubElement(tv, "channel", id=epg.channel), "display-name", lang="en").text = epg.channel
    for p in epg.programs:
        if p.status == "SKIPPED":
            continue
        programme = SubElement(
            tv, "programme", start=_xmltv_time(p.start), stop=_xmltv_time(p.end), channel=epg.channel
        )
        SubElement(programme, "title", lang="en").text = p.title
        for genre in p.genres:
            SubElement(programme, "category", lang="en").text = genre
        if p.content_id:
            SubElement(programme, "episode-num", system="fast-channels-content-id").text = p.content_id
        if p.rating:
            SubElement(SubElement(programme, "rating"), "value").text = p.rating
    body = tostring(tv, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n{body}\n'


def keys(channel: str) -> tuple[str, str]:
    """S3 keys of the JSON and XMLTV feeds of ``channel``."""
    return f"{EPG_PREFIX}{channel}.json", f"{EPG_PREFIX}{channel}.xml"


def write(epg: Epg, *, bucket: str) -> tuple[str, str]:
    """Upload both feeds of ``epg`` to ``bucket``; returns their keys."""
    json_key, xml_key = keys(epg.channel)
    s3 = client("s3")
    for key, body, content_type in (
        (json_key, epg.model_dump_json(), "application/json"),
        (xml_key, xmltv(epg), "application/xml"),
    ):
        s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType=content_type, CacheControl=CACHE_CONTROL)
    return json_key, xml_key


def delete(channel: str, *, bucket: str) -> None:
    """Remove both feeds of ``channel`` (deleting a missing key succeeds)."""
    client("s3").delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys(channel)], "Quiet": True}
    )
