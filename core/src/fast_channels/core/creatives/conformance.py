"""Ladder conformance of a creative's HLS (DESIGN-v2.2 §11, P0-09 c): what the own book may bid.

MediaTailor stitches a streaming creative that does not match the channel's ladder by nearest match, and never falls
back to its progressive file (a 360p rung got a 540p rendition at 2.08x its advertised bandwidth; FR-8). So a
creative is bid only when its multivariant playlist matches the ladder:

- **variants**: as many as the ladder's renditions, in its order, each with the rendition's codec family, resolution,
  frame rate and the muxed AAC audio (``core.manifests.expected_signature``);
- **bandwidth**: each variant's ``BANDWIDTH`` is at most its rendition's nominal peak (``Ladder.nominal_bandwidth``);
- **duration**: every variant lasts as long as the others, a whole number of ad slots of the ladder's grid
  (:data:`~fast_channels.core.timeline.DEMO_AD_GOPS`: 30.03 s at 29.97 fps), each within one frame.

The pipeline's own encodes of a creative pass the first two by construction (``Finalize`` checks the signature and
writes the nominal bandwidths); the check states them, so a ladder change or a foreign playlist cannot slip through.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.manifests import (
    expected_signature,
    ladder_signature,
    media_segment_durations,
    parse_multivariant,
)
from fast_channels.core.timeline import DEMO_AD_GOPS

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder


class Conformance(BaseModel):
    """The check's result: conformant when it found no problem."""

    model_config = ConfigDict(frozen=True)

    ladder_version: str
    variants: int = Field(ge=0)
    duration_ms: int | None = Field(default=None, description="The variants' duration, when they agree.")
    slots: int | None = Field(default=None, description="Ad slots of the grid the duration covers, when on the grid.")
    problems: tuple[str, ...] = ()

    @property
    def conformant(self) -> bool:
        return not self.problems


def slot_ms(ladder: Ladder) -> int:
    """One ad slot of the ladder's grid (30,030 ms at 29.97 fps with 2.002 s GOPs)."""
    return ladder.grid.gop_exact(DEMO_AD_GOPS)


def frame_ms(ladder: Ladder) -> float:
    """One frame of the ladder."""
    return 1000 / float(ladder.frame_rate.fraction)


def check(multivariant: str, media: Mapping[str, str], ladder: Ladder) -> Conformance:
    """Check a creative's ``multivariant`` playlist and its ``media`` playlists (by the multivariant's URIs) against
    ``ladder`` (module docstring)."""
    problems: list[str] = []
    variants = parse_multivariant(multivariant)
    expected, found = expected_signature(ladder), ladder_signature(multivariant)
    if len(found) != len(expected):
        problems.append(f"{len(found)} variants, the ladder has {len(expected)}")
    else:
        problems += [
            f"variant {n} is {'/'.join(row)}, the ladder's is {'/'.join(want)}"
            for n, (row, want) in enumerate(zip(found, expected, strict=True), start=1)
            if row != want
        ]
        for n, (variant, rendition) in enumerate(zip(variants, ladder.video, strict=True), start=1):
            peak, _ = ladder.nominal_bandwidth(rendition)
            if variant.bandwidth > peak:
                problems.append(f"variant {n} advertises {variant.bandwidth} bit/s, the rendition's peak is {peak}")
    durations: list[int] = []
    for variant in variants:
        if (text := media.get(variant.uri)) is None:
            problems.append(f"media playlist {variant.uri} is missing")
        elif not (segments := media_segment_durations(text)):
            problems.append(f"media playlist {variant.uri} has no segments")
        else:
            durations.append(round(sum(segments) * 1000))
    duration, slots = _duration(durations, ladder, problems)
    return Conformance(
        ladder_version=ladder.version,
        variants=len(variants),
        duration_ms=duration,
        slots=slots,
        problems=tuple(problems),
    )


def _duration(durations: list[int], ladder: Ladder, problems: list[str]) -> tuple[int | None, int | None]:
    """The variants' common duration and its slots of the grid (None when they disagree or it is off the grid)."""
    if not durations:
        if not problems:
            problems.append("no variants")
        return None, None
    frame, slot = frame_ms(ladder), slot_ms(ladder)
    duration = durations[0]
    if any(abs(other - duration) > frame for other in durations):
        problems.append(f"the variants last {sorted(set(durations))} ms, not one duration")
        return None, None
    slots = round(duration / slot)
    if slots < 1 or abs(duration - slots * slot) > frame:
        problems.append(f"{duration} ms is not a whole number of {slot} ms ad slots (to one frame)")
        return duration, None
    return duration, slots
