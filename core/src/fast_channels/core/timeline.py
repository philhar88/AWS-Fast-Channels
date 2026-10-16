"""Frame-exact timeline arithmetic and deterministic channel scheduling.

All times are integer milliseconds (:data:`Millis`). Frame rates are exact fractions, so a 29.97 fps GOP of
60 frames is exactly 2002 ms and a 15-GOP slate exactly 30030 ms (no drift over long schedules).

Scheduling follows Channel Assembly semantics verified in channel-assembly.md §5 and e2e.md §2.9: programs play
back to back, an ad break *inserts* its slate (a 60 s program with two 10 s breaks airs for 80 s), break offsets
are offsets into the VOD source, and every break is a TIME_SIGNAL whose ids reach the ad decision server.
"""

import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from itertools import pairwise
from typing import Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fast_channels.core.naming import mediatailor_name

type Millis = int

BREAK_SLATE_GOPS: Final[tuple[int, ...]] = (15, 30, 45, 60)
"""Ad-break slate lengths in GOPs (~30/60/90/120 s at 2 s GOPs). Channel Assembly sets a break's duration to its
slate's duration, so breaks can only have these lengths (channel-assembly.md §5.3)."""

HOUSE_AD_GOPS: Final[tuple[int, ...]] = (15, 15)
"""House-ad creative lengths in GOPs (~30 s each). Every break slate is a whole number of these, so a pod that cycles
through the house ads fills any break exactly: MediaTailor drops the whole avail when the time left unfilled exceeds
the playback configuration's personalization threshold (LEFTOVER_AVAIL_EXCEEDED_THRESHOLD, seen live with 14 + 30 s
ads in a 60 s break)."""

KIT_SPEC: Final = f"breaks={'+'.join(map(str, BREAK_SLATE_GOPS))};ads={'+'.join(map(str, HOUSE_AD_GOPS))}"
"""Identifies the kit's slate and house-ad lengths; part of a kit's fingerprint, so changing them re-provisions it."""

LEAD_TIME_MS: Final[Millis] = 10 * 60_000  # CA docs recommend adding VOD programs >= 10 min ahead (§5.3)
MAX_PROGRAMS: Final = 400  # Channel Assembly quota: programs per channel (§5.6)
UPID_FORMAT_ID: Final = "FST3"  # 4-byte MPU format identifier; MediaTailor strips it from [scte.segmentation_upid]
_NTSC: Final = Fraction(1000, 1001)


def _fraction(value: str | float) -> Fraction:
    text = str(value).strip()
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        return Fraction(int(numerator), int(denominator))
    exact = Fraction(Decimal(text))
    if exact.denominator == 1:
        return exact
    nominal = round(exact / _NTSC)  # 29.97 -> 30, 23.976 -> 24, 59.94 -> 60
    if abs(exact - nominal * _NTSC) < Fraction(1, 200):
        return nominal * _NTSC
    return exact.limit_denominator(1001)


class FrameRate(BaseModel):
    """Exact frame rate as a fraction; accepts ``"29.97"``, ``"30000/1001"``, ``25`` or ``29.97`` on input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: int = Field(gt=0)
    denominator: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        if isinstance(data, str | int | float) and not isinstance(data, bool):
            fraction = _fraction(data)
        elif isinstance(data, dict) and {"numerator", "denominator"} <= data.keys():
            fraction = Fraction(int(data["numerator"]), int(data["denominator"]))
        else:
            return data
        return {"numerator": fraction.numerator, "denominator": fraction.denominator}

    @classmethod
    def parse(cls, value: str | float) -> FrameRate:
        """Frame rate from a label (``"29.97"`` -> 30000/1001, ``"25"`` -> 25/1) or a ``"num/den"`` string."""
        return cls.model_validate(value)

    @property
    def fraction(self) -> Fraction:
        """Frames per second as an exact fraction."""
        return Fraction(self.numerator, self.denominator)

    @property
    def fps(self) -> float:
        """Frames per second."""
        return self.numerator / self.denominator

    def frames(self, seconds: float) -> int:
        """Number of whole frames in ``seconds`` (rounded to the nearest frame)."""
        return round(Fraction(seconds) * self.fraction)

    def frames_to_ms(self, frames: int) -> Millis:
        """Duration of ``frames`` frames in milliseconds, floored (exact for 23.976/25/29.97/30/50/59.94 GOPs)."""
        return frames * 1000 * self.denominator // self.numerator

    def __str__(self) -> str:
        return str(self.numerator) if self.denominator == 1 else f"{self.numerator}/{self.denominator}"


class Grid(BaseModel):
    """GOP and segment grid of a ladder: every rendition of every codec has an IDR on each GOP boundary."""

    model_config = ConfigDict(frozen=True)

    frame_rate: FrameRate
    gop_frames: int = Field(gt=0)
    segment_gops: int = Field(gt=0)

    @property
    def gop_ms(self) -> Millis:
        """Duration of one GOP (2002 ms at 29.97 fps with 60-frame GOPs)."""
        return self.gop_exact(1)

    @property
    def segment_ms(self) -> Millis:
        """Duration of one full segment (6006 ms at 29.97 fps)."""
        return self.gop_exact(self.segment_gops)

    def gop_exact(self, n: int) -> Millis:
        """Duration of exactly ``n`` GOPs, computed from frames so it never accumulates rounding drift."""
        return self.frame_rate.frames_to_ms(n * self.gop_frames)

    def gop_index(self, ms: Millis) -> int:
        """Index of the GOP boundary nearest to ``ms``."""
        rate = self.frame_rate
        return round(Fraction(ms * rate.numerator, 1000 * rate.denominator * self.gop_frames))

    def snap_to_gop(self, ms: Millis) -> Millis:
        """``ms`` moved to the nearest GOP boundary.

        Off-grid ESAM points split AVC/HEVC but not AV1 or audio, which truncates the DASH MPD, so every cue point
        is snapped before it reaches MediaConvert or Channel Assembly (mediaconvert.md §8.3).
        """
        return self.gop_exact(self.gop_index(ms))


class BreakPolicy(BaseModel):
    """How often ad breaks air, and where in a program they may go.

    A break is due ``cadence_minutes`` after the previous one ended (or the block started), and never comes less
    than ``min_gap_minutes`` after it: half the cadence when unset, so any explicit cadence is honoured.
    ``max_breaks`` limits breaks per program. The margins and ``min_program_minutes`` keep breaks at plain segment
    boundaries out of a program's opening and closing seconds and out of short programs; an asset's editorial cue
    points (:attr:`PlanInputAsset.editorial_breaks`) are used wherever the editor put them.
    """

    model_config = ConfigDict(extra="forbid")

    cadence_minutes: float = Field(12, gt=0)
    min_gap_minutes: float | None = Field(None, ge=0)
    max_breaks: int | None = Field(None, ge=0)
    slate_gops: int = 30
    start_margin_seconds: float = Field(60, ge=0)
    end_margin_seconds: float = Field(30, ge=0)
    min_program_minutes: float = Field(3, ge=0)

    @model_validator(mode="after")
    def _known_slate(self) -> Self:
        if self.slate_gops not in BREAK_SLATE_GOPS:
            raise ValueError(f"slate_gops must be one of {BREAK_SLATE_GOPS}")
        return self

    @property
    def cadence_ms(self) -> Millis:
        """Target time from the end of one break to the next."""
        return round(self.cadence_minutes * 60_000)

    @property
    def min_gap_ms(self) -> Millis:
        """Shortest time from the end of one break to the next: ``min_gap_minutes``, else half the cadence."""
        if self.min_gap_minutes is None:
            return self.cadence_ms // 2
        return round(self.min_gap_minutes * 60_000)

    def offsets(self, asset: PlanInputAsset) -> list[Millis]:
        """The offsets of ``asset`` a break may use: inside the content, and for plain segment boundaries also
        ``start_margin_seconds`` from its start, ``end_margin_seconds`` from its end and only in programs of at
        least ``min_program_minutes``."""
        inside = sorted({o for o in asset.break_offsets_ms if 0 < o < asset.duration_ms})
        if asset.editorial_breaks:
            return inside
        if asset.duration_ms < round(self.min_program_minutes * 60_000):
            return []
        first = round(self.start_margin_seconds * 1000)
        last = asset.duration_ms - round(self.end_margin_seconds * 1000)
        return [o for o in inside if first <= o <= last]


_HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class Daypart(BaseModel):
    """A UTC time-of-day window (``start`` inclusive, ``end`` exclusive, may wrap midnight) with content rules.

    Programs *starting* inside the window must match ``ratings`` and ``genres`` when those are non-empty.
    """

    model_config = ConfigDict(extra="forbid")

    start: str = Field(pattern=_HHMM)
    end: str = Field(pattern=_HHMM)
    ratings: list[str] = []
    genres: list[str] = []

    def contains(self, moment: time) -> bool:
        """Whether a UTC time of day falls inside this window (``start == end`` means all day)."""
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if start < end:
            return start <= moment < end
        return moment >= start or moment < end


class ProgrammingBrief(BaseModel):
    """A channel's programming rules: allowed ratings and genres, dayparts and ad-break policy."""

    model_config = ConfigDict(extra="forbid")

    ratings: list[str] = []
    genres_allow: list[str] = []
    genres_deny: list[str] = []
    dayparts: list[Daypart] = []
    break_policy: BreakPolicy = BreakPolicy()
    notes: str = ""


class PlannedBreak(BaseModel):
    """An ad break at ``offset_ms`` into the program's VOD source, airing the ``slate_gops`` slate."""

    offset_ms: Millis
    duration_ms: Millis
    slate_gops: int
    event_id: int
    upid: str


class PlannedProgram(BaseModel):
    """One program in a channel schedule. ``duration_ms`` is the on-air duration, i.e. content plus breaks."""

    name: str
    asset_id: str
    vod_source: str
    content_id: str | None
    start_ms: Millis
    duration_ms: Millis
    breaks: list[PlannedBreak]
    ladder_version: str

    @property
    def end_ms(self) -> Millis:
        """When the program stops airing."""
        return self.start_ms + self.duration_ms


class ViolationCode(StrEnum):
    """Violation codes reported by :func:`plan_block` and :func:`validate_plan`."""

    EMPTY = "EMPTY"
    START_IN_PAST = "START_IN_PAST"
    LEAD_TIME = "LEAD_TIME"
    LADDER_MISMATCH = "LADDER_MISMATCH"
    OVERLAP = "OVERLAP"
    GAP = "GAP"
    NAME_CONFLICT = "NAME_CONFLICT"
    MAX_PROGRAMS = "MAX_PROGRAMS"
    BREAK_OFFSET = "BREAK_OFFSET"
    RATING = "RATING"
    GENRE = "GENRE"
    DAYPART = "DAYPART"


class Violation(BaseModel):
    """A rule a plan breaks. Errors block publishing; warnings are informational."""

    code: str
    severity: Literal["error", "warning"]
    message: str
    program: str | None = None


class Plan(BaseModel):
    """A computed block of programs for one channel, with every rule it violates."""

    channel: str
    ladder_version: str
    programs: list[PlannedProgram]
    violations: list[Violation]
    start_ms: Millis
    end_ms: Millis

    @property
    def ok(self) -> bool:
        """True when no violation has error severity."""
        return all(v.severity != "error" for v in self.violations)


class PlanInputAsset(BaseModel):
    """A schedulable asset: a registered VOD source encoded with ``ladder_version``.

    ``break_offsets_ms`` are the offsets where every track has a common segment boundary (see
    :func:`fast_channels.core.manifests.segment_boundaries_ms`). When the asset has editorial cue points the pipeline
    keeps only the boundaries at those points: set ``editorial_breaks`` then, so the planner trusts their placement
    instead of applying the :class:`BreakPolicy` margins.
    """

    asset_id: str
    vod_source: str
    content_id: str | None = None
    duration_ms: Millis = Field(gt=0)
    break_offsets_ms: list[Millis] = []
    editorial_breaks: bool = False
    ladder_version: str
    rating: str | None = None
    genres: list[str] = []


def program_name(start_ms: Millis, asset_id: str) -> str:
    """Deterministic Channel Assembly program name ``<yyyymmddThhmmss>-<asset_id>`` (UTC, at most 64 chars)."""
    stamp = datetime.fromtimestamp(start_ms / 1000, UTC).strftime("%Y%m%dT%H%M%S")
    return mediatailor_name(stamp, asset_id)


def scte_event_id(program: str, index: int) -> int:
    """Deterministic 31-bit SCTE-35 segmentation event id for break ``index`` of ``program`` (never 0)."""
    value = int.from_bytes(hashlib.sha256(f"{program}/{index}".encode()).digest()[:4]) & 0x7FFFFFFF
    return value or 1


def upid(content_id: str) -> str:
    """Hex SCTE-35 MPU UPID (type 0x0C) payload: format id ``FST3`` followed by the content id bytes."""
    payload = f"{UPID_FORMAT_ID}{content_id}".encode()
    if len(payload) > 255:  # segmentation_upid_length is an 8-bit field
        raise ValueError("content id is too long for a SCTE-35 UPID")
    return payload.hex().upper()


def _error(code: ViolationCode, message: str, program: str | None = None) -> Violation:
    return Violation(code=code, severity="error", message=message, program=program)


def _warning(code: ViolationCode, message: str, program: str | None = None) -> Violation:
    return Violation(code=code, severity="warning", message=message, program=program)


def _content_violations(asset: PlanInputAsset, brief: ProgrammingBrief, name: str, start_ms: Millis) -> list[Violation]:
    found: list[Violation] = []
    genres = set(asset.genres)
    if brief.ratings and asset.rating not in brief.ratings:
        found.append(_error(ViolationCode.RATING, f"rating {asset.rating!r} is not allowed on this channel", name))
    if denied := genres & set(brief.genres_deny):
        found.append(_error(ViolationCode.GENRE, f"genres {sorted(denied)} are denied on this channel", name))
    if brief.genres_allow and not genres & set(brief.genres_allow):
        found.append(_error(ViolationCode.GENRE, f"none of {sorted(genres)} is an allowed genre", name))
    moment = datetime.fromtimestamp(start_ms / 1000, UTC).time()
    for part in brief.dayparts:
        if not part.contains(moment):
            continue
        window = f"{part.start}-{part.end}"
        if part.ratings and asset.rating not in part.ratings:
            found.append(_error(ViolationCode.DAYPART, f"rating {asset.rating!r} not allowed in {window}", name))
        if part.genres and not genres & set(part.genres):
            found.append(_error(ViolationCode.DAYPART, f"genres {sorted(genres)} not allowed in {window}", name))
    return found


def _plan_breaks(
    name: str, asset: PlanInputAsset, *, start_ms: Millis, grid: Grid, policy: BreakPolicy, last_break_end: Millis
) -> tuple[list[PlannedBreak], Millis]:
    """Pick breaks from the offsets the policy allows (:meth:`BreakPolicy.offsets`) nearest to the cadence.

    The cadence runs across program boundaries within a block, and a break is never closer than the policy's
    minimum gap to the end of the previous one. Returns the breaks and the new last-break end.
    """
    cadence, min_gap = policy.cadence_ms, policy.min_gap_ms
    slate_ms = grid.gop_exact(policy.slate_gops)
    content_id = asset.content_id or asset.asset_id
    candidates = policy.offsets(asset)
    offsets: list[Millis] = []
    while policy.max_breaks is None or len(offsets) < policy.max_breaks:
        inserted = len(offsets) * slate_ms
        due = last_break_end + cadence - start_ms - inserted  # content offset where the next break is due
        if due >= asset.duration_ms:
            break
        after = offsets[-1] if offsets else 0
        eligible = [o for o in candidates if o > after and start_ms + o + inserted - last_break_end >= min_gap]
        if not eligible:
            break
        offset = min(eligible, key=lambda o: (abs(o - due), o))
        offsets.append(offset)
        last_break_end = start_ms + offset + inserted + slate_ms
    breaks = [
        PlannedBreak(
            offset_ms=o,
            duration_ms=slate_ms,
            slate_gops=policy.slate_gops,
            event_id=scte_event_id(name, i),
            upid=upid(content_id),
        )
        for i, o in enumerate(offsets)
    ]
    return breaks, last_break_end


def plan_block(
    *,
    channel: str,
    ladder_version: str,
    grid: Grid,
    start_ms: Millis,
    assets: Sequence[PlanInputAsset],
    brief: ProgrammingBrief,
    now_ms: Millis,
    existing: Sequence[PlannedProgram] = (),
) -> Plan:
    """Schedule ``assets`` back to back from ``start_ms`` and report every rule the result violates.

    The model only picks assets and their order; this function does all the arithmetic: program names, starts and
    on-air durations, break offsets and SCTE ids. ``grid`` is the channel ladder's grid (it fixes slate durations).
    ``existing`` is the channel's current schedule, used for overlap, gap and quota checks.
    """
    programs: list[PlannedProgram] = []
    violations: list[Violation] = []
    cursor = last_break_end = start_ms
    for asset in assets:
        name = program_name(cursor, asset.asset_id)
        violations += _content_violations(asset, brief, name, cursor)
        breaks, last_break_end = _plan_breaks(
            name, asset, start_ms=cursor, grid=grid, policy=brief.break_policy, last_break_end=last_break_end
        )
        duration = asset.duration_ms + sum(b.duration_ms for b in breaks)
        programs.append(
            PlannedProgram(
                name=name,
                asset_id=asset.asset_id,
                vod_source=asset.vod_source,
                content_id=asset.content_id,
                start_ms=cursor,
                duration_ms=duration,
                breaks=breaks,
                ladder_version=asset.ladder_version,
            )
        )
        cursor += duration
    plan = Plan(
        channel=channel,
        ladder_version=ladder_version,
        programs=programs,
        violations=[],
        start_ms=start_ms,
        end_ms=cursor,
    )
    return plan.model_copy(update={"violations": violations + validate_plan(plan, now_ms=now_ms, existing=existing)})


def _overlaps(a: PlannedProgram, b: PlannedProgram) -> bool:
    return a.start_ms < b.end_ms and b.start_ms < a.end_ms


def _break_violations(program: PlannedProgram) -> Iterable[Violation]:
    content_ms = program.duration_ms - sum(b.duration_ms for b in program.breaks)
    offsets = [b.offset_ms for b in program.breaks]
    if offsets != sorted(set(offsets)) or any(not 0 <= o < content_ms for o in offsets):
        yield _error(
            ViolationCode.BREAK_OFFSET, "break offsets must be unique, ascending and inside the content", program.name
        )


def validate_plan(plan: Plan, *, now_ms: Millis, existing: Sequence[PlannedProgram] = ()) -> list[Violation]:
    """Time- and schedule-dependent checks; re-run before publishing to detect a stale plan.

    Lead time under 10 minutes is a warning (late inserts work, channel-assembly.md §5.3); a start in the past,
    overlaps, name clashes, ladder mismatches and the 400-program quota are errors.
    """
    found: list[Violation] = []
    if not plan.programs:
        found.append(_error(ViolationCode.EMPTY, "the plan has no programs"))
    lead = plan.start_ms - now_ms
    if lead <= 0:
        found.append(_error(ViolationCode.START_IN_PAST, "the block starts in the past"))
    elif lead < LEAD_TIME_MS:
        found.append(_warning(ViolationCode.LEAD_TIME, f"starts in {lead // 1000} s; best effort below 10 min"))
    names = {e.name for e in existing}
    for program in plan.programs:
        if program.ladder_version != plan.ladder_version:
            found.append(
                _error(
                    ViolationCode.LADDER_MISMATCH,
                    f"encoded with {program.ladder_version}, channel uses {plan.ladder_version}",
                    program.name,
                )
            )
        if program.name in names:
            found.append(_error(ViolationCode.NAME_CONFLICT, "a program with this name already exists", program.name))
        found += [
            _error(ViolationCode.OVERLAP, f"overlaps scheduled program {e.name}", program.name)
            for e in existing
            if _overlaps(program, e)
        ]
        found += _break_violations(program)
    for a, b in pairwise(plan.programs):
        if b.start_ms != a.end_ms:
            found.append(
                _error(
                    ViolationCode.OVERLAP if b.start_ms < a.end_ms else ViolationCode.GAP,
                    f"does not start when {a.name} ends",
                    b.name,
                )
            )
    before = [e.end_ms for e in existing if e.end_ms <= plan.start_ms]
    if before and (gap := plan.start_ms - max(before)) > 0:
        found.append(_warning(ViolationCode.GAP, f"{gap // 1000} s of filler slate before the block"))
    if (total := len(existing) + len(plan.programs)) > MAX_PROGRAMS:
        found.append(_error(ViolationCode.MAX_PROGRAMS, f"{total} programs exceed the {MAX_PROGRAMS} quota"))
    return found
