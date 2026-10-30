"""Ad prefetch on the monetized siblings (DESIGN §10, DECISIONS 2026-10-30): rolling, create-only SINGLE schedules,
one per upcoming break of the channel, matched on ``scte.event_id``.

The Release Controller's prefetcher builds them from Channel Assembly's schedule of record:

1. :func:`breaks_of`: each break of the next hours from ``GetChannelSchedule`` (its approximate start and length)
   and the program's ``AdBreaks`` (``DescribeProgram``: the SCTE-35 event id and MPU UPID our schedule tools set);
2. :func:`plan`: against the sibling's existing schedules (``ListPrefetchSchedules`` with ``ScheduleType=ALL``),
   the ``CreatePrefetchSchedule`` requests of the next ``horizon_breaks`` breaks that have none, and the schedules
   to delete: those whose break left the schedule (a re-plan; retrieval transcodes bill whether or not the break
   airs) or moved (no update exists, so a moved break's schedule is deleted and created again). Once a schedule's
   retrieval has opened, its break has moved only if it left the schedule's consumption window: MediaTailor puts
   the retrieved ads in the first break of that window whose ``scte.event_id`` matches, so a shift of Channel
   Assembly's start by a second or two keeps them (deleting the schedule threw them away and sent every session to a
   second auction at the break, fastcij 2026-10-30).

Every schedule carries rule R-S1's ``DynamicVariables`` (``scte.event_id``, ``scte.segmentation_upid``,
``session.avail_duration_ms``) and ``session.avail_duration_secs``, which the POST body's stored-request id uses.
Retrieval opens ``retrieval_lead_s`` before the break and spreads the sessions' ADS requests over
``shaping_window_s`` (``RETRIEVAL_WINDOW`` traffic shaping), so the PDS sees a ramp instead of a break-time herd. A
configuration with a RECURRING schedule gets no SINGLE schedule: the two are never mixed (P0-02). A break without a
UPID, or whose length is not one of the schedule's break lengths, is left to real-time insertion.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.monetization.playback import RELEASE_OWNER
from fast_channels.core.monetization.validators import check_prefetch_schedule
from fast_channels.core.specs import UPID_TYPE_MPU
from fast_channels.core.tags import TAG_APP, TAG_OWNER
from fast_channels.core.timeline import UPID_FORMAT_ID

NAME_PREFIX: Final = "pf-"
"""Every schedule the prefetcher creates is ``pf-<event id>``; it never touches another."""
MATCHED_ON: Final = "scte.event_id"
ACTIVE_LIMIT: Final = 25
"""MediaTailor's quota of active prefetch schedules per playback configuration (expired ones do not count)."""
MOVED_TOLERANCE: Final = timedelta(seconds=3)
"""Half a 6.006 s content segment. Before its retrieval opens, a break whose start moved by more than this, compared at
the 1 s resolution MediaTailor stores schedule times at (P0-02), gets its schedule deleted and created again."""
LENGTH_TOLERANCE_MS: Final = 1000
"""``ApproximateDurationSeconds`` is whole seconds: a break is the schedule length within a second of it."""
_NAME: Final = re.compile(rf"{re.escape(NAME_PREFIX)}(?P<event_id>[0-9]{{1,10}})")


class PrefetchPolicy(BaseModel):
    """When a break's ads are retrieved and consumed (``modules.release.prefetch``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    horizon_breaks: int = Field(default=20, ge=1, le=ACTIVE_LIMIT - 1)
    """Upcoming breaks with a schedule, at most; one schedule stays free under the quota of 25."""
    retrieval_lead_s: int = Field(default=150, ge=60, le=3600)
    """Retrieval opens this long before the break."""
    retrieval_end_s: int = Field(default=30, ge=10)
    """Retrieval closes this long before the break, leaving time for the transcodes (about 20 s, P0-02)."""
    shaping_window_s: int = Field(default=90, ge=0)
    """``RETRIEVAL_WINDOW`` traffic shaping: the sessions' ADS requests spread over this many seconds; 0 sends them
    as the sessions arrive."""
    consumption_lead_s: int = Field(default=20, ge=0)
    """Consumption opens this long before the break."""
    consumption_tail_s: int = Field(default=10, ge=0)
    """Consumption closes this long after the break ends."""
    min_retrieval_s: int = Field(default=30, ge=10)
    """A break whose retrieval would stay open for less than this from now is left to real-time insertion."""


@dataclass(frozen=True, slots=True)
class Break:
    """One upcoming break of the channel's schedule of record."""

    event_id: int
    upid: str
    """The title's content id (the MPU UPID without ``FST3``), as MediaTailor passes it in real time."""
    start: datetime
    duration_ms: int
    program: str

    @property
    def name(self) -> str:
        """Its schedule's name on every sibling."""
        return schedule_name(self.event_id)


def schedule_name(event_id: int) -> str:
    """``pf-<event id>``."""
    return f"{NAME_PREFIX}{event_id}"


def event_id_of(name: str) -> int | None:
    """The event id of one of the prefetcher's schedules, None for any other name."""
    match = _NAME.fullmatch(name)
    return int(match["event_id"]) if match else None


def content_id_of_upid(upid_type: int | None, upid_hex: str | None) -> str:
    """The content id an MPU UPID (``FST3`` + id, hex) carries; "" for another type or format."""
    if upid_type != UPID_TYPE_MPU or not upid_hex:
        return ""
    try:
        payload = bytes.fromhex(upid_hex).decode()
    except ValueError:  # not hex, or not UTF-8
        return ""
    return payload.removeprefix(UPID_FORMAT_ID) if payload.startswith(UPID_FORMAT_ID) else ""


def breaks_of(
    entries: Iterable[Mapping[str, Any]],
    programs: Mapping[str, Mapping[str, Any]],
    *,
    breaks_ms: Sequence[int],
) -> tuple[list[Break], list[str]]:
    """The breaks of ``GetChannelSchedule`` items, with the ids of their programs' ``AdBreaks``, and what was left
    out and why.

    Args:
        entries: the schedule's items, in any order.
        programs: ``DescribeProgram`` answers by program name.
        breaks_ms: the stage's break lengths (the default ladder's slate grid), which the approximate lengths snap to.
    """
    found: list[Break] = []
    skipped: list[str] = []
    for entry in entries:
        if entry.get("ScheduleEntryType", "PROGRAM") != "PROGRAM" or not entry.get("ScheduleAdBreaks"):
            continue
        name = entry["ProgramName"]
        if (program := programs.get(name)) is None:
            skipped.append(f"{name}: not described")
            continue
        aired = sorted(entry["ScheduleAdBreaks"], key=lambda b: b["ApproximateStartTime"])
        signals = sorted(program.get("AdBreaks") or [], key=lambda b: b.get("OffsetMillis", 0))
        if len(aired) != len(signals):
            skipped.append(f"{name}: {len(aired)} scheduled breaks, {len(signals)} ad breaks")
            continue
        for n, (slot, signal) in enumerate(zip(aired, signals, strict=True), start=1):
            event_id, upid = _ids(signal)
            length = _snap(int(slot["ApproximateDurationSeconds"]) * 1000, breaks_ms)
            if event_id is None or not upid or length is None:
                skipped.append(f"{name} break {n}: no event id, UPID or schedule length")
                continue
            found.append(Break(event_id, upid, slot["ApproximateStartTime"], length, name))
    return sorted(found, key=lambda b: b.start), skipped


def _ids(signal: Mapping[str, Any]) -> tuple[int | None, str]:
    descriptors = (signal.get("TimeSignalMessage") or {}).get("SegmentationDescriptors") or []
    if descriptors:
        first = descriptors[0]
        upid = content_id_of_upid(first.get("SegmentationUpidType"), first.get("SegmentationUpid"))
        return first.get("SegmentationEventId"), upid
    splice = signal.get("SpliceInsertMessage") or {}
    return splice.get("SpliceEventId"), ""


def _snap(length_ms: int, breaks_ms: Sequence[int]) -> int | None:
    nearest = min(breaks_ms, key=lambda ms: abs(ms - length_ms), default=None)
    return nearest if nearest is not None and abs(nearest - length_ms) <= LENGTH_TOLERANCE_MS else None


def schedule_request(
    item: Break, *, configuration: str, policy: PrefetchPolicy, now: datetime, tags: Mapping[str, str]
) -> dict[str, Any]:
    """The ``CreatePrefetchSchedule`` arguments of ``item``'s schedule on ``configuration``."""
    retrieval_start = max(item.start - timedelta(seconds=policy.retrieval_lead_s), now)
    retrieval_end = item.start - timedelta(seconds=policy.retrieval_end_s)
    open_s = int((retrieval_end - retrieval_start).total_seconds())
    retrieval: dict[str, Any] = {
        "StartTime": retrieval_start,
        "EndTime": retrieval_end,
        "DynamicVariables": {
            MATCHED_ON: str(item.event_id),
            "scte.segmentation_upid": item.upid,
            "session.avail_duration_ms": str(item.duration_ms),
            "session.avail_duration_secs": str(round(item.duration_ms / 1000)),
        },
    }
    if policy.shaping_window_s:
        retrieval["TrafficShapingType"] = "RETRIEVAL_WINDOW"
        retrieval["TrafficShapingRetrievalWindow"] = {
            "RetrievalWindowDurationSeconds": min(policy.shaping_window_s, open_s)
        }
    return {
        "Name": item.name,
        "PlaybackConfigurationName": configuration,
        "ScheduleType": "SINGLE",
        "Retrieval": retrieval,
        "Consumption": {
            "StartTime": item.start - timedelta(seconds=policy.consumption_lead_s),
            "EndTime": item.start + timedelta(milliseconds=item.duration_ms, seconds=policy.consumption_tail_s),
            "AvailMatchingCriteria": [{"DynamicVariable": MATCHED_ON, "Operator": "EQUALS"}],
        },
        "Tags": dict(tags),
    }


def schedule_tags(prefix: str) -> dict[str, str]:
    """``fast:app`` and ``fast:owner=release-controller``: the reaper deletes only what carries both."""
    return {TAG_APP: prefix, TAG_OWNER: RELEASE_OWNER}


@dataclass(frozen=True, slots=True)
class Plan:
    """What the prefetcher does on one configuration."""

    configuration: str
    create: list[dict[str, Any]] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    refused: str | None = None
    """Why nothing is created (a RECURRING schedule is there)."""
    left_out: list[str] = field(default_factory=list)
    """Breaks of the horizon that get no schedule, and why."""


def plan(
    breaks: Sequence[Break],
    existing: Iterable[Mapping[str, Any]],
    *,
    configuration: str,
    policy: PrefetchPolicy,
    now: datetime,
    tags: Mapping[str, str],
) -> Plan:
    """The schedules to create and delete on ``configuration`` (module docstring)."""
    items = list(existing)
    if recurring := [i["Name"] for i in items if i.get("ScheduleType") == "RECURRING"]:
        return Plan(configuration, refused=f"RECURRING schedules {recurring}: never mixed with SINGLE ones")
    active = {i["Name"]: i for i in items if event_id_of(i["Name"]) is not None and i["Consumption"]["EndTime"] > now}
    scheduled = {b.event_id: b for b in breaks}
    delete = []
    for name, schedule in active.items():
        found = scheduled.get(event_id_of(name))  # type: ignore[arg-type]  # active holds only pf-<event id> names
        if found is None or _moved(schedule, found, policy, now):
            delete.append(name)
    remaining = len(active) - len(delete)
    create: list[dict[str, Any]] = []
    kept: list[str] = []
    left_out: list[str] = []
    soonest = now + timedelta(seconds=policy.retrieval_end_s + policy.min_retrieval_s)
    for upcoming in [b for b in breaks if b.start >= soonest][: policy.horizon_breaks]:
        if upcoming.name in active and upcoming.name not in delete:
            kept.append(upcoming.name)
            continue
        if remaining >= ACTIVE_LIMIT - 1:
            left_out.append(f"{upcoming.name}: {remaining} active schedules")
            continue
        request = schedule_request(upcoming, configuration=configuration, policy=policy, now=now, tags=tags)
        if violations := check_prefetch_schedule(request):
            left_out.append(f"{upcoming.name}: {violations[0].message}")
            continue
        create.append(request)
        remaining += 1
    return Plan(configuration, create=create, delete=sorted(delete), kept=kept, left_out=left_out)


def _stored(at: datetime) -> datetime:
    """``at`` at the resolution MediaTailor keeps schedule times at (whole seconds, P0-02)."""
    return at.replace(microsecond=0)


def _moved(schedule: Mapping[str, Any], scheduled: Break, policy: PrefetchPolicy, now: datetime) -> bool:
    """Whether ``scheduled`` no longer fits ``schedule``: once retrieval has opened, only a break that left the
    consumption window; before, one that moved by more than :data:`MOVED_TOLERANCE` (whole seconds)."""
    consumption = schedule["Consumption"]
    opens = (schedule.get("Retrieval") or {}).get("StartTime")
    if opens is None or opens <= now:  # retrieved, or retrieving: the window decides (no StartTime: retrieved at once)
        return not consumption["StartTime"] <= scheduled.start <= consumption["EndTime"]
    expected = _stored(scheduled.start - timedelta(seconds=policy.consumption_lead_s))
    return abs(_stored(consumption["StartTime"]) - expected) > MOVED_TOLERANCE
