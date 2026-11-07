"""Pacing of the own book's lines (M9's sales agent): spend share over elapsed share, judged once a flight is under
way."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from fast_channels.core.pds.book import Line
from fast_channels.core.pds.pacing import PaceState, pacing

STARTS = datetime(2026, 11, 1, tzinfo=UTC)
ENDS = STARTS + timedelta(days=10)


def line(*, flight: bool = True) -> Line:
    return Line(
        id="direct-1",
        advertiser="Direct Advertiser 1",
        adomain="direct1.example",
        creatives=("direct1",),
        cpm=Decimal(8),
        budget=Decimal(100),
        starts=STARTS if flight else None,
        ends=ENDS if flight else None,
    )


@pytest.mark.parametrize(
    ("day", "left", "state", "pace"),
    [
        (5, "50", PaceState.ON_PACE, 1.0),
        (5, "35", PaceState.OVER, 1.3),
        (5, "62", PaceState.UNDER, 0.76),
        (1, "40", PaceState.OVER, 6.0),
        (0.25, "90", PaceState.SETTLING, 4.0),
        (-1, "100", PaceState.NOT_STARTED, None),
        (10, "0", PaceState.ENDED, None),
    ],
)
def test_a_lines_pace_is_its_spend_share_over_its_elapsed_share(
    day: float, left: str, state: PaceState, pace: float | None
) -> None:
    found = pacing(line(), Decimal(left), STARTS + timedelta(days=day))

    assert found.state is state
    assert found.pace == pytest.approx(pace) if pace is not None else found.pace is None
    assert found.spent == Decimal(100) - Decimal(left)
    assert found.state.off_pace is (state in {PaceState.OVER, PaceState.UNDER})


def test_a_line_without_a_flight_or_unread_shards_has_no_pace() -> None:
    at = STARTS + timedelta(days=5)

    assert pacing(line(flight=False), Decimal(10), at).state is PaceState.NO_FLIGHT
    unread = pacing(line(), None, at)
    assert (unread.state, unread.spent, unread.pace) == (PaceState.UNREAD, None, None)


def test_spend_never_counts_below_zero() -> None:
    assert pacing(line(), Decimal(120), STARTS + timedelta(days=5)).spent == 0
