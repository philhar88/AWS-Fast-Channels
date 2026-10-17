from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from fast_channels.core.timeline import (
    BREAK_SLATE_GOPS,
    HOUSE_AD_GOPS,
    KIT_SPEC,
    LEAD_TIME_MS,
    BreakPolicy,
    Daypart,
    FrameRate,
    Grid,
    Plan,
    PlanInputAsset,
    PlannedBreak,
    PlannedProgram,
    ProgrammingBrief,
    ViolationCode,
    plan_block,
    program_name,
    scte_event_id,
    upid,
    validate_plan,
)

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

T0 = int(datetime(2026, 10, 16, 12, 0, tzinfo=UTC).timestamp() * 1000)
MINUTE = 60_000


@pytest.mark.parametrize(
    ("value", "fraction"),
    [
        ("29.97", (30000, 1001)),
        (29.97, (30000, 1001)),
        ("23.976", (24000, 1001)),
        ("23.98", (24000, 1001)),
        ("59.94", (60000, 1001)),
        ("25", (25, 1)),
        (30, (30, 1)),
        ("50", (50, 1)),
        ("30000/1001", (30000, 1001)),
        ("60/2", (30, 1)),
        ("12.5", (25, 2)),
    ],
)
def test_frame_rate_parse(value: str | float, fraction: tuple[int, int]) -> None:
    rate = FrameRate.parse(value)
    assert (rate.numerator, rate.denominator) == fraction


def test_frame_rate_arithmetic() -> None:
    ntsc = FrameRate.parse("29.97")
    assert ntsc.fps == pytest.approx(29.97, abs=1e-4)
    assert ntsc.frames(2) == 60
    assert ntsc.frames_to_ms(60) == 2002
    assert ntsc.frames_to_ms(1) == 33  # floored
    assert str(ntsc) == "30000/1001"
    assert str(FrameRate.parse(25)) == "25"
    assert FrameRate.model_validate({"numerator": 50, "denominator": 2}) == FrameRate.parse(25)
    with pytest.raises(ValidationError):
        FrameRate.parse("0")
    with pytest.raises(ValidationError):
        FrameRate.model_validate({"numerator": 30})


@pytest.fixture
def grid() -> Grid:
    return Grid(frame_rate=FrameRate.parse("29.97"), gop_frames=60, segment_gops=3)


def test_grid_is_frame_exact(grid: Grid) -> None:
    assert (grid.gop_ms, grid.segment_ms) == (2002, 6006)
    assert grid.gop_exact(15) == 30030  # a "30 s" slate at 29.97
    assert grid.gop_exact(150) == 300300  # no drift over long offsets
    assert grid.gop_exact(3 * 599) == 599 * grid.segment_ms == 3_597_594  # an hour of segments, still exact


def test_cue_points_snap_to_the_segment_grid(grid: Grid) -> None:
    # Basic tier has no ESAM, so the only boundaries every variant shares are the 6006 ms segment boundaries.
    assert grid.snap_to_segment(20000) == 18018
    assert grid.snap_to_segment(40000) == 42042
    assert grid.snap_to_segment(254254) == 252252  # a GOP boundary is not a segment boundary
    assert (grid.segment_index(4100), grid.snap_to_segment(4100)) == (1, 6006)
    assert grid.snap_to_segment(0) == grid.snap_to_segment(3002) == 0
    assert grid.snap_to_segment(3004) == 6006
    for ms in range(0, 400_000, 7):
        snapped = grid.snap_to_segment(ms)
        assert snapped % 6006 == 0 and abs(snapped - ms) <= 3003  # at most half a segment away
        assert grid.snap_to_segment(snapped) == snapped
    pal = Grid(frame_rate=FrameRate.parse("25"), gop_frames=50, segment_gops=3)
    assert (pal.segment_ms, pal.snap_to_segment(20000), pal.segment_index(20000)) == (6000, 18000, 3)


def test_break_catalogue_constants() -> None:
    assert BREAK_SLATE_GOPS == (15, 30, 45, 60)
    assert HOUSE_AD_GOPS == (15, 15)


def test_break_policy_only_known_slates() -> None:
    assert BreakPolicy().slate_gops == 30
    with pytest.raises(ValidationError, match="slate_gops"):
        BreakPolicy(slate_gops=20)


def test_daypart_windows() -> None:
    evening = Daypart(start="18:00", end="22:00")
    assert evening.contains(time(18, 0)) and not evening.contains(time(22, 0))
    night = Daypart(start="22:00", end="06:00")
    assert night.contains(time(23, 30)) and night.contains(time(5, 59)) and not night.contains(time(12, 0))
    assert Daypart(start="00:00", end="00:00").contains(time(13, 37))
    with pytest.raises(ValidationError):
        Daypart(start="24:00", end="01:00")


def test_program_name_is_deterministic_and_bounded() -> None:
    assert program_name(T0, "big-buck-bunny-1a2b3c") == "20261016T120000-big-buck-bunny-1a2b3c"
    assert len(program_name(T0, "a" * 80)) == 64


def test_scte_ids_and_upid() -> None:
    first = scte_event_id("p1", 0)
    assert first == scte_event_id("p1", 0) != scte_event_id("p1", 1)
    assert 0 < first < 2**31
    assert upid("p1b2") == "4653543370316232"  # "FST3p1b2", the UPID verified end to end (e2e.md §2.9)
    with pytest.raises(ValueError, match="too long"):
        upid("x" * 252)


def _asset(n: int, minutes: float = 22, **kwargs: object) -> PlanInputAsset:
    duration = round(minutes * MINUTE)
    fields: dict[str, object] = {
        "asset_id": f"episode-{n}",
        "vod_source": f"fast-episode-{n}",
        "content_id": f"ep{n}",
        "duration_ms": duration,
        "break_offsets_ms": list(range(6006, duration, 6006)),
        "ladder_version": "standard@0123abcd45",
        "rating": "TV-G",
        "genres": ["animation"],
    }
    return PlanInputAsset.model_validate(fields | kwargs)


def _plan(
    assets: list[PlanInputAsset],
    grid: Grid,
    brief: ProgrammingBrief | None = None,
    existing: tuple[PlannedProgram, ...] = (),
    now_ms: int = T0 - 20 * MINUTE,
) -> Plan:
    return plan_block(
        channel="fast-demo",
        ladder_version="standard@0123abcd45",
        grid=grid,
        start_ms=T0,
        assets=assets,
        brief=brief or ProgrammingBrief(),
        now_ms=now_ms,
        existing=existing,
    )


def test_plan_block_schedules_back_to_back_with_cadenced_breaks(grid: Grid) -> None:
    plan = _plan([_asset(1), _asset(2)], grid)
    assert plan.ok and plan.violations == []
    first, second = plan.programs
    assert first.name == "20261016T120000-episode-1"
    assert [b.offset_ms for b in first.breaks] == [720720]  # 120 segments = 12:00.72, nearest to the 12 min cadence
    slate = first.breaks[0]
    assert (slate.duration_ms, slate.slate_gops, slate.upid) == (60060, 30, upid("ep1"))
    assert slate.event_id == scte_event_id(first.name, 0)
    assert first.duration_ms == 22 * MINUTE + 60060  # Channel Assembly inserts the slate
    assert second.start_ms == first.end_ms
    # The cadence runs across programs: the next break is due 12 min after the first one ended.
    due = first.start_ms + 720720 + 60060 + 12 * MINUTE
    assert abs(second.start_ms + second.breaks[0].offset_ms - due) <= grid.segment_ms
    assert plan.end_ms == second.end_ms


def test_short_programs_still_get_breaks(grid: Grid) -> None:
    plan = _plan([_asset(n, minutes=5) for n in range(4)], grid)
    assert sum(len(p.breaks) for p in plan.programs) == 1
    assert plan.programs[2].breaks  # due at 12 min, i.e. 2 min into the third 5-minute program


def test_breaks_respect_min_gap_and_max_breaks(grid: Grid) -> None:
    brief = ProgrammingBrief(break_policy=BreakPolicy(cadence_minutes=2, min_gap_minutes=6, max_breaks=2))
    breaks = _plan([_asset(1, minutes=40)], grid, brief).programs[0].breaks
    assert len(breaks) == 2
    assert breaks[0].offset_ms >= 6 * MINUTE  # min gap from the block start
    assert breaks[1].offset_ms - breaks[0].offset_ms >= 6 * MINUTE  # and between break end and next break
    assert _plan([_asset(1, break_offsets_ms=[])], grid).programs[0].breaks == []


def _offsets(plan: Plan) -> list[list[int]]:
    return [[b.offset_ms for b in p.breaks] for p in plan.programs]


def test_short_programs_get_no_breaks_at_plain_boundaries(grid: Grid) -> None:
    # The live incident: a break due 6 s into the 52 s trailer that follows an 11.9-minute title.
    trailer = _asset(2, minutes=52 / 60)
    assert _offsets(_plan([_asset(1, minutes=11.9), trailer], grid)) == [[], []]
    anywhere = ProgrammingBrief(break_policy=BreakPolicy(min_program_minutes=0))
    assert _offsets(_plan([_asset(1, minutes=11.9), trailer], grid, anywhere)) == [[], []]  # 52 s < both margins


def test_breaks_keep_clear_of_program_start_and_end(grid: Grid) -> None:
    due_early = _plan([_asset(1, minutes=11.5), _asset(2, minutes=5)], grid)  # due 30 s into the second program
    assert _offsets(due_early) == [[], [60060]]  # the first segment boundary at least 60 s in
    late = ProgrammingBrief(break_policy=BreakPolicy(cadence_minutes=4.9, max_breaks=1))  # due 6 s before the end
    assert _offsets(_plan([_asset(1, minutes=5)], grid, late)) == [[264264]]  # the last boundary 30 s before it
    unbounded = BreakPolicy(cadence_minutes=4.9, max_breaks=1, start_margin_seconds=0, end_margin_seconds=0)
    assert _offsets(_plan([_asset(1, minutes=5)], grid, ProgrammingBrief(break_policy=unbounded))) == [[294294]]


def test_editorial_cue_points_are_honoured_anywhere(grid: Grid) -> None:
    trailer = _asset(1, minutes=52 / 60, break_offsets_ms=[grid.snap_to_segment(20000)], editorial_breaks=True)
    brief = ProgrammingBrief(break_policy=BreakPolicy(cadence_minutes=0.5))
    assert _offsets(_plan([trailer], grid, brief)) == [[18018]]  # 18 s in, in a 52 s program


def test_min_gap_defaults_to_half_the_cadence(grid: Grid) -> None:
    assert BreakPolicy().min_gap_ms == 6 * MINUTE
    assert BreakPolicy(cadence_minutes=5).min_gap_ms == 150_000
    assert BreakPolicy(cadence_minutes=5, min_gap_minutes=0).min_gap_ms == 0
    # Tears of Steel: cue points 3.9 minutes apart, both kept at a 5-minute cadence (a fixed 6-minute gap dropped one).
    cues = [grid.snap_to_segment(ms) for ms in (254254, 488488)]
    tears = _asset(1, minutes=734 / 60, break_offsets_ms=cues, editorial_breaks=True)
    brief = ProgrammingBrief(break_policy=BreakPolicy(cadence_minutes=5))
    plan = _plan([tears], grid, brief)
    assert _offsets(plan) == [[252252, 486486]]
    assert plan == _plan([tears], grid, brief)  # deterministic


def test_content_rules(grid: Grid) -> None:
    brief = ProgrammingBrief(
        ratings=["TV-G"],
        genres_allow=["animation", "family"],
        genres_deny=["horror"],
        dayparts=[
            Daypart(start="11:00", end="13:00", ratings=["TV-Y"], genres=["family"]),
            Daypart(start="20:00", end="23:00", ratings=["TV-MA"]),
        ],
    )
    plan = _plan([_asset(1, rating="TV-14", genres=["horror"])], grid, brief)
    codes = sorted(v.code for v in plan.violations)
    assert codes == ["DAYPART", "DAYPART", "GENRE", "GENRE", "RATING"]
    assert not plan.ok
    assert all(v.program == plan.programs[0].name for v in plan.violations)


def test_schedule_checks(grid: Grid) -> None:
    other = _plan([_asset(9)], grid).programs[0]
    mismatched = _asset(2, ladder_version="custom@0123abcd45")
    plan = _plan([_asset(1), mismatched], grid, existing=(other,), now_ms=T0 - MINUTE)
    found = {(v.code, v.severity) for v in plan.violations}
    assert ("LEAD_TIME", "warning") in found
    assert ("LADDER_MISMATCH", "error") in found
    assert ("OVERLAP", "error") in found
    later = other.model_copy(update={"name": "earlier", "start_ms": T0 - 30 * MINUTE, "duration_ms": 20 * MINUTE})
    gap = validate_plan(_plan([_asset(1)], grid), now_ms=T0 - LEAD_TIME_MS, existing=[later])
    assert [(v.code, v.message) for v in gap] == [("GAP", "600 s of filler slate before the block")]


def test_validate_plan_errors(grid: Grid) -> None:
    plan = _plan([_asset(1), _asset(2)], grid)
    assert [v.code for v in validate_plan(plan, now_ms=T0)] == [ViolationCode.START_IN_PAST]
    assert ViolationCode.EMPTY in [v.code for v in _plan([], grid).violations]
    clash = plan.programs[0].model_copy(update={"start_ms": T0 - 30 * MINUTE})
    assert "NAME_CONFLICT" in [v.code for v in validate_plan(plan, now_ms=0, existing=[clash])]
    crowded = [
        plan.programs[0].model_copy(update={"name": f"old-{i}", "start_ms": i, "duration_ms": 1}) for i in range(399)
    ]
    assert "MAX_PROGRAMS" in [v.code for v in validate_plan(plan, now_ms=0, existing=crowded)]
    first, second = plan.programs
    overlapping = plan.model_copy(update={"programs": [first, second.model_copy(update={"start_ms": first.start_ms})]})
    gapped = plan.model_copy(update={"programs": [first, second.model_copy(update={"start_ms": second.end_ms})]})
    assert "OVERLAP" in [v.code for v in validate_plan(overlapping, now_ms=0)]
    assert "GAP" in [v.code for v in validate_plan(gapped, now_ms=0)]
    bad_break = PlannedBreak(offset_ms=first.duration_ms, duration_ms=0, slate_gops=30, event_id=1, upid="00")
    broken = plan.model_copy(update={"programs": [first.model_copy(update={"breaks": [bad_break]})]})
    assert [v.code for v in validate_plan(broken, now_ms=0)] == ["BREAK_OFFSET"]


def test_plan_uses_the_ladder_grid(standard: Ladder) -> None:
    plan = plan_block(
        channel="c",
        ladder_version="standard@0123abcd45",
        grid=standard.grid,
        start_ms=T0,
        assets=[_asset(1)],
        brief=ProgrammingBrief(),
        now_ms=T0 - LEAD_TIME_MS,
    )
    assert plan.programs[0].breaks[0].duration_ms == standard.grid.gop_exact(30)
    assert Plan.model_validate(plan.model_dump(mode="json")) == plan


def test_every_break_slate_is_a_whole_number_of_house_ads() -> None:
    """A pod cycling through the house ads fills every break exactly (no LEFTOVER_AVAIL_EXCEEDED_THRESHOLD)."""
    cycle = sum(HOUSE_AD_GOPS)
    for gops in BREAK_SLATE_GOPS:
        filled, ads = 0, iter(HOUSE_AD_GOPS * gops)
        while filled < gops:
            filled += next(ads)
        assert filled == gops, (gops, cycle)
    assert KIT_SPEC == "breaks=15+30+45+60;ads=15+15"
