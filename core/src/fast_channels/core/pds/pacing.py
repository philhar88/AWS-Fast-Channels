"""Pacing of the own book's lines (M9, the sales agent): how a line's spend compares with how much of its flight is
gone.

A line's spend is what its budget shards no longer have (``cap - remaining`` summed over its shards: reserved and
delivered alike, ``core.pds.delivery``). Its pace is the share of the budget spent over the share of the flight
elapsed: 1.0 spends evenly to the end of the flight, above :data:`OVER` spends the budget early, below :data:`UNDER`
leaves it unspent. Only a line with a whole flight (``starts`` and ``ends``) has a pace; early in a flight
(:data:`SETTLING`) its pace says too little to act on.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fast_channels.core.pds.book import Line

OVER: Final = 1.2
UNDER: Final = 0.8
SETTLING: Final = 0.05
"""The share of a flight that must be gone before a pace is judged."""


class PaceState(StrEnum):
    ON_PACE = "on_pace"
    OVER = "overpacing"
    UNDER = "underpacing"
    SETTLING = "settling"
    """Too early in the flight to judge."""
    NOT_STARTED = "not_started"
    ENDED = "ended"
    NO_FLIGHT = "no_flight"
    """No start or end: the line serves until its budget is spent, and has no pace."""
    UNREAD = "unread"
    """Its budget shards could not be read."""

    @property
    def off_pace(self) -> bool:
        """Whether a human (or the sales agent's proposal) should look at the line."""
        return self in {PaceState.OVER, PaceState.UNDER}


@dataclass(frozen=True, slots=True)
class Pacing:
    """One line's pacing at ``at``."""

    line: str
    advertiser: str
    budget: Decimal
    spent: Decimal | None
    starts: datetime | None
    ends: datetime | None
    elapsed: float | None
    """The share of the flight gone, 0 to 1."""
    spent_share: float | None
    pace: float | None
    state: PaceState


def pacing(line: Line, remaining: Decimal | None, at: datetime) -> Pacing:
    """``line``'s pacing at ``at``, from what its budget shards have left (None: unread)."""
    spent = None if remaining is None else max(line.budget - remaining, Decimal(0))
    spent_share = None if spent is None else float(spent / line.budget)

    def done(state: PaceState, elapsed: float | None = None, pace: float | None = None) -> Pacing:
        return Pacing(
            line.id, line.advertiser, line.budget, spent, line.starts, line.ends, elapsed, spent_share, pace, state
        )

    if line.starts is None or line.ends is None:
        return done(PaceState.NO_FLIGHT)
    if at < line.starts:
        return done(PaceState.NOT_STARTED, 0.0)
    if at >= line.ends:
        return done(PaceState.ENDED, 1.0)
    elapsed = (at - line.starts) / (line.ends - line.starts)
    if spent_share is None:
        return done(PaceState.UNREAD, elapsed)
    pace = spent_share / elapsed if elapsed > 0 else None
    if elapsed < SETTLING or pace is None:
        return done(PaceState.SETTLING, elapsed, pace)
    state = PaceState.OVER if pace > OVER else PaceState.UNDER if pace < UNDER else PaceState.ON_PACE
    return done(state, elapsed, pace)
