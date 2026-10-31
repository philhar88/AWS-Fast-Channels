"""Matching as-run ``AdBreakStart`` events to scheduled breaks, on a live run's plan and as-run log (2026-10-27): 4
programs, 24 planned breaks, the channel stopped after the 20th."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.as_run import MATCH_TOLERANCE_MS, break_air_times, matching_break, unaired_breaks
from fast_channels.core.timeline import PlannedBreak, scte_event_id

RUN: dict[str, Any] = json.loads((Path(__file__).parent / "data" / "as-run-2026-10-27.json").read_text())
PROGRAMS = {p["name"]: p for p in RUN["programs"]}
BREAKS = {name: [PlannedBreak(**b) for b in p["breaks"]] for name, p in PROGRAMS.items()}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def events(kind: str) -> list[dict[str, Any]]:
    return [e for e in RUN["as_run"] if e["eventType"] == kind]


def program_starts() -> dict[str, int]:
    return {e["programName"]: ms(e["timeStamp"]) for e in events("VodProgramStart")}


def test_the_run() -> None:
    assert len(PROGRAMS) == 4
    assert sum(len(b) for b in BREAKS.values()) == 24
    assert len(events("AdBreakStart")) == 20
    assert set(program_starts()) == set(PROGRAMS)


def test_every_break_start_matches_its_break_exactly_from_the_program_start() -> None:
    starts = program_starts()
    matched = []
    for event in events("AdBreakStart"):
        name, at = event["programName"], ms(event["timeStamp"])
        index = matching_break(at, starts[name], BREAKS[name])
        assert index is not None, event
        assert break_air_times(starts[name], BREAKS[name])[index] == at  # to the millisecond
        matched.append((name, index))

    assert len(set(matched)) == 20  # each aired break once
    assert [i for name, i in matched if name == RUN["programs"][0]["name"]] == list(range(6))


def test_the_planned_start_is_0959_ms_early_and_still_matches() -> None:
    starts = program_starts()
    for event in events("AdBreakStart"):
        name, at = event["programName"], ms(event["timeStamp"])
        planned = PROGRAMS[name]["start_ms"]
        index = matching_break(at, planned, BREAKS[name])

        assert index is not None
        assert at - break_air_times(planned, BREAKS[name])[index] == 959
        assert starts[name] - planned == 959


def test_the_event_id_of_a_matched_break_is_the_schedules() -> None:
    name = RUN["programs"][0]["name"]
    [first] = [e for e in events("AdBreakStart") if e["programName"] == name][:1]
    index = matching_break(ms(first["timeStamp"]), program_starts()[name], BREAKS[name])

    assert index == 0
    assert BREAKS[name][0].event_id == scte_event_id(name, 0) == 1817010633


def test_the_four_breaks_after_the_stop_never_aired() -> None:
    starts, stopped = program_starts(), ms(RUN["stopped_at"])
    aired: dict[str, set[int]] = {name: set() for name in PROGRAMS}
    for event in events("AdBreakStart"):
        name = event["programName"]
        index = matching_break(ms(event["timeStamp"]), starts[name], BREAKS[name])
        assert index is not None
        aired[name].add(index)
    later = stopped + 3600_000

    unaired = {name: unaired_breaks(later, starts[name], BREAKS[name], aired[name]) for name in PROGRAMS}

    assert sum(len(i) for i in unaired.values()) == 4
    assert unaired[RUN["programs"][3]["name"]] == [2, 3, 4, 5]
    assert all(not i for name, i in unaired.items() if name != RUN["programs"][3]["name"])


def test_a_break_is_not_unaired_before_its_time_has_passed() -> None:
    name = RUN["programs"][0]["name"]
    start = program_starts()[name]
    first_end = break_air_times(start, BREAKS[name])[0] + BREAKS[name][0].duration_ms

    assert unaired_breaks(first_end, start, BREAKS[name], set()) == []
    assert unaired_breaks(first_end + 10 * 60_000 + 1, start, BREAKS[name], set()) == [0]
    assert unaired_breaks(first_end + 10 * 60_000 + 1, start, BREAKS[name], {0}) == []


@pytest.mark.parametrize("delta", [MATCH_TOLERANCE_MS + 1, -(MATCH_TOLERANCE_MS + 1)])
def test_an_event_far_from_every_break_matches_none(delta: int) -> None:
    name = RUN["programs"][0]["name"]
    start = program_starts()[name]
    first = break_air_times(start, BREAKS[name])[0]

    assert matching_break(first + delta, start, BREAKS[name][:1]) is None
    assert matching_break(first, start, []) is None
