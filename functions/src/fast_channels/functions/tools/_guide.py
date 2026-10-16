"""A channel's guide from the media library's ``PROGRAM`` items: guide entries, the published EPG feeds and gaps."""

from collections.abc import Sequence
from itertools import pairwise
from typing import Final

from pydantic import BaseModel

from fast_channels.functions import epg
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import library_store as store

EPG_HISTORY_MS: Final = 6 * 3600_000
"""How far back the published feeds reach."""
PROGRAM_LOOKBACK_MS: Final = 86_400_000
"""Programs starting more than a day before a moment have long finished at it."""


class Gap(BaseModel):
    """Filler time between two published programs."""

    start_ms: int
    start: str
    end_ms: int
    end: str
    duration_ms: int


def guide_entry(program: store.ProgramRecord) -> epg.EpgProgram:
    return epg.EpgProgram(
        name=program.name,
        title=program.title or program.content_id or program.asset_id,
        content_id=program.content_id,
        asset_id=program.asset_id,
        start=store.iso(program.start_ms),
        end=store.iso(program.end_ms),
        rating=program.rating,
        genres=program.genres,
        breaks=[epg.EpgBreak(offset_ms=b.offset_ms, duration_ms=b.duration_ms) for b in program.breaks],
        status=program.status.value,
    )


def recent_programs(channel: str, at_ms: int) -> list[store.ProgramRecord]:
    """Programs of ``channel`` that end after ``at_ms``, in start order."""
    return [p for p in store.programs(channel, start_ms=max(0, at_ms - PROGRAM_LOOKBACK_MS)) if p.end_ms > at_ms]


def write_guide(channel: store.ChannelRecord, at_ms: int) -> epg.Epg:
    """Render and upload ``epg/<channel>.{json,xml}`` from the media library."""
    guide = epg.feed(
        channel=channel.name,
        ladder_version=channel.ladder_version,
        programs=[guide_entry(p) for p in recent_programs(channel.name, at_ms - EPG_HISTORY_MS)],
        generated_at=store.iso(at_ms),
    )
    config = settings()
    config.require("media_bucket")
    epg.write(guide, bucket=config.media_bucket)
    return guide


def gaps(programs: Sequence[store.ProgramRecord], after_ms: int) -> list[Gap]:
    """Filler stretches between consecutive programs that end after ``after_ms``."""
    found = []
    for a, b in pairwise(programs):
        start, end = max(a.end_ms, after_ms), b.start_ms
        if end > start:
            found.append(
                Gap(start_ms=start, start=store.iso(start), end_ms=end, end=store.iso(end), duration_ms=end - start)
            )
    return found
