"""Which scheduled break an as-run ``AdBreakStart`` is, and which breaks never aired.

Channel Assembly's as-run log has one ``AdBreakStart`` per break and output, with the program name and the on-air
time but no break id or duration. A program's breaks air at its start plus each break's offset into the content plus
the breaks before it. In a live run of 20 breaks in 4 programs (2026-10-27), every ``AdBreakStart`` came exactly
there when the program's own ``VodProgramStart`` time is the start, and a constant 0.959 s after the planned time
otherwise (the first program snapped to the filler's segment grid). So the break is the one whose on-air time is
nearest, its SCTE-35 event id is the schedule's, and a break that did not start well after its time never aired:
Channel Assembly writes no event for a break it skips, for example after the channel stopped.

Channel Assembly also loses the ``AdBreakStart`` of a break that went on air shortly before a ``StopChannel``
(W-2 1.31): :func:`stopped_after` finds those breaks from the channel's stops.
"""

from collections.abc import Collection, Iterable, Sequence
from itertools import accumulate
from typing import Final, Protocol

MATCH_TOLERANCE_MS: Final = 30_000
"""An ``AdBreakStart`` further than this from every break's on-air time belongs to none (a program published
outside the schedule tools, or a schedule changed after publishing)."""
UNAIRED_AFTER_MS: Final = 10 * 60_000
"""How long after a break's planned end its start may still be logged; later, it never aired."""
# OPTIMIZATION(aws): W-2 1.31 Channel Assembly loses the AdBreakStart of a break on air shortly before StopChannel
STOP_LOSS_MS: Final = 60_000
"""How long before a ``StopChannel`` a break may have gone on air and still have no ``AdBreakStart``. Channel
Assembly writes as-run events in batches 13 to 50 s after they happen, and a stop loses the batch in flight: stops
3.5 s and 25 s after a break's start left no event, and one 20 s after the start kept it (2026-10-29)."""


class Break(Protocol):
    """A scheduled break: where it sits in the content and how long it airs (``timeline.PlannedBreak``)."""

    @property
    def offset_ms(self) -> int: ...

    @property
    def duration_ms(self) -> int: ...


def break_air_times(start_ms: int, breaks: Sequence[Break]) -> list[int]:
    """On-air start of each of a program's ``breaks`` when the program starts at ``start_ms``."""
    before = accumulate((b.duration_ms for b in breaks), initial=0)
    return [start_ms + b.offset_ms + aired for b, aired in zip(breaks, before, strict=False)]


def matching_break(
    at_ms: int, start_ms: int, breaks: Sequence[Break], *, tolerance_ms: int = MATCH_TOLERANCE_MS
) -> int | None:
    """Index of the break that an ``AdBreakStart`` at ``at_ms`` is (the nearest on-air time), or ``None`` when none
    is within ``tolerance_ms``."""
    times = break_air_times(start_ms, breaks)
    if not times:
        return None
    index = min(range(len(times)), key=lambda i: abs(times[i] - at_ms))
    return index if abs(times[index] - at_ms) <= tolerance_ms else None


def unaired_breaks(
    now_ms: int, start_ms: int, breaks: Sequence[Break], decided: Collection[int], *, after_ms: int = UNAIRED_AFTER_MS
) -> list[int]:
    """Indexes of ``breaks`` that should have ended more than ``after_ms`` before ``now_ms`` and were not decided
    (marked aired or unaired) yet: they never aired."""
    return [
        i
        for i, (time, brk) in enumerate(zip(break_air_times(start_ms, breaks), breaks, strict=True))
        if i not in decided and time + brk.duration_ms + after_ms < now_ms
    ]


def stopped_after(
    start_ms: int,
    breaks: Sequence[Break],
    pending: Iterable[int],
    stops_ms: Collection[int],
    *,
    within_ms: int = STOP_LOSS_MS,
) -> dict[int, int]:
    """The ``pending`` breaks that went on air at most ``within_ms`` before a stop of their channel, each with the
    first such stop: they aired, although the as-run log may have no ``AdBreakStart`` for them (W-2 1.31). A break
    due after a stop never aired, so it is not one of them."""
    times = break_air_times(start_ms, breaks)
    found: dict[int, int] = {}
    for index in pending:
        if after := [stop for stop in stops_ms if 0 <= stop - times[index] <= within_ms]:
            found[index] = min(after)
    return found
