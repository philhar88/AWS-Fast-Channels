"""``schedule`` tools: read a channel's guide, plan a block, publish it to Channel Assembly, edit the brief.

The MediaLibrary ``PROGRAM`` items are the schedule of record (Channel Assembly deletes aired programs about 15
minutes after they finish, channel-assembly.md §5.6). Planning is pure arithmetic in ``core.timeline.plan_block``;
publishing replays a stored plan against the live schedule:

* one writer per channel: a conditional ``LEASE`` item with a TTL (concurrent edits raise ConflictException);
* re-anchoring: the block is linked after the live program that precedes it *in time*, because list order beats
  wall-clock time for ABSOLUTE programs (channel-assembly.md §5.3);
* ``PLAN_STALE`` when the live schedule from the anchor on changed since planning (fingerprint of that tail), or an
  asset of the plan is no longer READY with the VOD source it was planned with;
* CreateProgram strictly sequential at ≤ 3 TPS (``channel_ops``), each program recorded in the media library as
  soon as Channel Assembly has it: a publish that fails part-way leaves the guide in step with what airs, and
  deterministic program names let the same plan be published again to resume.
"""

import hashlib
import json
import math
import secrets
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Self

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field, ValidationError, model_validator

from fast_channels.core import specs
from fast_channels.core.catalog import ContentItem
from fast_channels.core.events import SchedulePublished, put_events_entry
from fast_channels.core.ingest import AssetKind
from fast_channels.core.timeline import (
    BREAK_SLATE_GOPS,
    LEAD_TIME_MS,
    BreakPolicy,
    Daypart,
    Plan,
    PlanInputAsset,
    PlannedProgram,
    ProgrammingBrief,
    Violation,
    ViolationCode,
    plan_block,
    validate_plan,
)
from fast_channels.functions import aws, epg
from fast_channels.functions.channel_ops import (
    LiveProgram,
    channel_arn,
    create_programs,
    delete_programs,
    error_code,
    live_programs,
)
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools._alerts import Alert, after_change
from fast_channels.functions.tools._guide import Gap, gaps, guide_entry, recent_programs, write_guide
from fast_channels.functions.tools.library_store import ChannelInput, require_channel
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, describe_fields, tool

logger = Logger()

describe_fields(
    BreakPolicy,
    cadence_minutes="Target minutes from the end of one ad break to the next, counted across programs.",
    min_gap_minutes="Minimum minutes between the end of one break and the next break; omit for half the cadence.",
    max_breaks="Most breaks per program; omit for no limit.",
    slate_gops=f"Break length in GOPs, one of {', '.join(map(str, BREAK_SLATE_GOPS))}: about 30, 60, 90 or 120 s "
    "with 2 s GOPs.",
    start_margin_seconds="No break this close to a program's start, unless the asset has editorial cue points.",
    end_margin_seconds="No break this close to a program's end, unless the asset has editorial cue points.",
    min_program_minutes="Programs shorter than this get no breaks, unless the asset has editorial cue points.",
)
describe_fields(
    ProgrammingBrief,
    ratings="Allowed content ratings, e.g. TV-G and TV-PG; empty allows every rating.",
    genres_allow="A program needs at least one of these genres; empty allows every genre.",
    genres_deny="Programs with any of these genres are refused.",
    dayparts="Extra rules for programs that start inside UTC time-of-day windows.",
    break_policy="Ad-break policy of every plan on the channel; a plan can override it.",
    notes="Free-text guidance for the agent; the tools do not check it.",
)
describe_fields(
    Daypart,
    start="UTC start of the window, HH:MM (inclusive).",
    end="UTC end of the window, HH:MM (exclusive); may wrap past midnight, equal to start means all day.",
    ratings="Ratings allowed for programs starting in the window; empty adds no rating rule.",
    genres="A program starting in the window needs one of these genres; empty adds no genre rule.",
)

PLAN_TTL_S: Final = 3600
LEASE_TTL_S: Final = 360
"""Longer than the schedule function's 5-minute timeout, so a crashed publish frees the channel by itself."""
GC_WINDOW_MS: Final = 15 * 60_000  # aired programs linger in Channel Assembly ~15 min (channel-assembly.md §5.6)
MAX_PICKS: Final = 100
PUBLISH_TIME_MARGIN_MS: Final = 30_000
"""A publish stops creating programs this long before the function times out (a create can back off for 15 s)."""
RESUME_HINT: Final = (
    "The programs published so far are in the guide (schedule___get_schedule). Call schedule___publish again with "
    "the same plan_id to publish the rest, or schedule___remove_programs to take them off the schedule."
)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _epoch_ms(moment: datetime) -> int:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return round(aware.timestamp() * 1000)


# --- shared models ---------------------------------------------------------------------------------------------------


class BriefOutput(BaseModel):
    channel: str
    brief: ProgrammingBrief
    default: bool = Field(description="True when the channel has no brief yet and the defaults apply.")


# --- live schedule (Channel Assembly) --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a block goes in Channel Assembly's schedule list."""

    after: str | None
    """The program that precedes the block in time."""
    before: str | None
    """The first program of the schedule, when nothing precedes the block."""
    fingerprint: str
    """Digest of the live schedule from ``after`` on: publishing refuses a plan whose fingerprint changed."""


def anchor(live: Sequence[LiveProgram], start_ms: int, *, exclude: Iterable[str] = ()) -> Anchor:
    """Anchor of a block starting at ``start_ms`` in the live schedule.

    ``exclude`` (the block's own programs) keeps a partially published plan's anchor stable for a retry.
    """
    skip = set(exclude)
    others = [p for p in live if p.name not in skip]
    index = max((i for i, p in enumerate(others) if p.start_ms < start_ms), default=None)
    digest = hashlib.sha256("\n".join(p.name for p in others[index:]).encode()).hexdigest()[:16]
    if index is None:
        return Anchor(after=None, before=others[0].name if others else None, fingerprint=digest)
    return Anchor(after=others[index].name, before=None, fingerprint=digest)


# --- MediaLibrary helpers --------------------------------------------------------------------------------------------


@contextmanager
def lease(channel: str, owner: str) -> Iterator[None]:
    """Exclusive schedule-write lease on ``channel`` for the duration of the block."""
    key = store.lease_key(channel)
    now_s = int(time.time())
    try:
        store.put_item(
            {**key, "owner": owner, "expires_at": now_s + LEASE_TTL_S},
            condition=Attr("pk").not_exists() | Attr("expires_at").lt(now_s),
        )
    except ClientError as error:
        if error_code(error) != "ConditionalCheckFailedException":
            raise
        raise ToolError(
            "CONFLICT",
            f"another schedule change on {channel} is in progress",
            retryable=True,
            hint="Wait a minute, then plan again from the fresh schedule.",
        ) from None
    try:
        yield
    finally:
        try:
            store.table().delete_item(Key=key, ConditionExpression=Attr("owner").eq(owner))
        except ClientError as error:
            if error_code(error) != "ConditionalCheckFailedException":
                raise


# --- get_schedule ----------------------------------------------------------------------------------------------------


class ScheduleOutput(epg.Epg):
    """The channel guide (CONTRACTS §I) from now on, plus where the published schedule ends and its gaps."""

    end_ms: int | None = Field(description="End of the last published program; null when nothing is scheduled.")
    end: str | None
    gaps: list[Gap] = Field(description="Filler slate between published programs.")


@tool("schedule", access=Access.READ)
def get_schedule(payload: ChannelInput, ctx: ToolContext) -> ScheduleOutput:
    """Get a channel's published schedule from now on: programs with UTC start/end, title, rating and ad breaks,
    the end of the schedule (end; schedule___plan appends there by default) and the filler gaps between
    programs."""
    channel = require_channel(payload.channel)
    at = now_ms()
    upcoming = recent_programs(channel.name, at)
    last = upcoming[-1].end_ms if upcoming else None
    return ScheduleOutput(
        channel=channel.name,
        generated_at=store.iso(at),
        ladder_version=channel.ladder_version,
        programs=[guide_entry(p) for p in upcoming],
        end_ms=last,
        end=store.iso(last) if last is not None else None,
        gaps=gaps(upcoming, at),
    )


# --- plan ------------------------------------------------------------------------------------------------------------


class PlanInput(BaseModel):
    channel: str = Field(description="Channel name from channels___list_channels.", min_length=1, max_length=64)
    picks: list[str] = Field(
        min_length=1,
        max_length=MAX_PICKS,
        description="Asset ids in air order: READY content encoded with the channel's ladder version "
        "(library___find_assets). An id listed twice airs twice.",
    )
    start: datetime | None = Field(
        None,
        description="UTC ISO-8601 start of the block, e.g. 2026-10-16T18:00:00Z. Omit start and after to append "
        "to the published schedule.",
    )
    after: str | None = Field(
        None,
        description="Name of an upcoming program, from schedule___get_schedule, that the block starts right after.",
    )
    break_policy: BreakPolicy | None = Field(
        None, description="Ad-break policy for this block only; omit to use the channel brief's."
    )

    @model_validator(mode="after")
    def _one_anchor(self) -> Self:
        if self.start is not None and self.after is not None:
            raise ValueError("give start or after, not both")
        return self


class PlannedRow(PlannedProgram):
    """A planned program with its UTC times and catalog metadata."""

    start: str
    end: str
    title: str
    rating: str | None
    genres: list[str]


class PlanOutput(BaseModel):
    """A computed plan (``core.timeline.Plan`` fields plus ``plan_id``). Publish it with schedule___publish."""

    plan_id: str
    channel: str
    ladder_version: str
    start_ms: int
    start: str
    end_ms: int
    end: str
    programs: list[PlannedRow]
    violations: list[Violation]
    publishable: bool = Field(description="False while any violation has error severity.")
    expires_at: str = Field(description="The plan can be published until this UTC time.")


class Metadata(BaseModel):
    """Display metadata and origin of a planned asset."""

    title: str
    rating: str | None = None
    genres: list[str] = []
    source_location: str | None = None


class StoredPlan(BaseModel):
    """``CHANNEL#<name>`` / ``PLAN#<plan_id>``: what publish replays."""

    plan_id: str
    plan: Plan
    metadata: dict[str, Metadata]
    fingerprint: str
    correlation_id: str
    expires_at: int

    @property
    def channel(self) -> str:
        return self.plan.channel

    def save(self) -> None:
        store.put_item({**store.plan_key(self.channel, self.plan_id), **self.model_dump(mode="json")})

    @classmethod
    def load(cls, plan_id: str) -> Self:
        channel, _, token = plan_id.rpartition(".")
        item = store.get_item(store.plan_key(channel, plan_id)) if channel and token else None
        if item is None or item.get("expires_at", 0) < time.time():
            raise ToolError(
                "NOT_FOUND", f"plan {plan_id} does not exist or expired", hint="Call schedule___plan again."
            )
        return cls.model_validate(item)


def new_plan_id(channel: str) -> str:
    """``<channel>.<hex>``: publish needs only the id, and the channel keys the plan item."""
    return f"{channel}.{secrets.token_hex(4)}"


def _catalog(content_ids: Iterable[str]) -> dict[str, ContentItem]:
    """Reference-catalog items by content id (ratings and genres for the brief checks)."""
    config = settings()
    if not config.catalog_table:
        return {}
    catalog, found = aws.table(config.catalog_table), {}
    for content_id in dict.fromkeys(content_ids):
        if item := catalog.get_item(Key={"pk": f"CONTENT#{content_id}", "sk": "META"}).get("Item"):
            try:
                found[content_id] = ContentItem.model_validate(store.plain(item))
            except ValidationError:
                logger.warning("unreadable catalog item", extra={"content_id": content_id})
    return found


def _violation(code: str, message: str, asset_id: str) -> Violation:
    return Violation(code=code, severity="error", message=message, program=asset_id)


def _unschedulable(record: store.Asset | None, asset_id: str, ladder_version: str) -> Violation | None:
    if record is None:
        versions = sorted(a.ladder_version for a in store.asset_versions(asset_id))
        if versions:
            return _violation(
                ViolationCode.LADDER_MISMATCH,
                f"encoded only with {', '.join(versions)}; the channel plays {ladder_version}",
                asset_id,
            )
        return _violation("UNKNOWN_ASSET", "no such asset in the media library", asset_id)
    if record.kind is not AssetKind.CONTENT:
        return _violation("NOT_CONTENT", f"a {record.kind} asset cannot be scheduled as a program", asset_id)
    if record.status is not store.AssetStatus.READY or not record.vod_source or not record.duration_ms:
        return _violation("NOT_READY", f"asset is {record.status}, not READY with a VOD source", asset_id)
    return None


def resolve_picks(
    picks: Sequence[str], ladder_version: str
) -> tuple[list[PlanInputAsset], dict[str, Metadata], list[Violation]]:
    """Schedulable assets in pick order, their metadata, and a violation for every pick that is not schedulable."""
    records: dict[str, store.Asset] = {}
    violations = []
    for asset_id in dict.fromkeys(picks):
        record = store.get_asset(asset_id, ladder_version)
        if problem := _unschedulable(record, asset_id, ladder_version):
            violations.append(problem)
        elif record is not None:
            records[asset_id] = record
    catalog = _catalog(r.content_id for r in records.values() if r.content_id)
    metadata: dict[str, Metadata] = {}
    inputs: dict[str, PlanInputAsset] = {}
    for asset_id, r in records.items():
        item = catalog.get(r.content_id or "")
        metadata[asset_id] = Metadata(
            title=r.title or (item.title if item else asset_id),
            rating=item.rating if item else None,
            genres=item.genres if item else [],
            source_location=r.source_location,
        )
        inputs[asset_id] = PlanInputAsset(
            asset_id=asset_id,
            vod_source=r.vod_source or "",
            content_id=r.content_id,
            duration_ms=r.duration_ms or 0,
            break_offsets_ms=r.break_offsets_ms,
            editorial_breaks=bool(r.cue_points_ms),
            ladder_version=r.ladder_version,
            rating=metadata[asset_id].rating,
            genres=metadata[asset_id].genres,
        )
    return [inputs[a] for a in picks if a in inputs], metadata, violations


def _block_start(payload: PlanInput, upcoming: Sequence[store.ProgramRecord], at_ms: int) -> int:
    if payload.start is not None:
        return _epoch_ms(payload.start)
    if payload.after is not None:
        previous = next((p for p in upcoming if p.name == payload.after), None)
        if previous is None:
            raise ToolError(
                "NOT_FOUND",
                f"{payload.after} is not an upcoming program of {payload.channel}",
                hint="Use a program name from schedule___get_schedule.",
            )
        return previous.end_ms
    if upcoming:
        return upcoming[-1].end_ms
    return math.ceil((at_ms + LEAD_TIME_MS) / 1000) * 1000


def plan_output(stored: StoredPlan) -> PlanOutput:
    plan = stored.plan
    rows = [
        PlannedRow(
            **p.model_dump(),
            start=store.iso(p.start_ms),
            end=store.iso(p.end_ms),
            **stored.metadata[p.asset_id].model_dump(exclude={"source_location"}),
        )
        for p in plan.programs
    ]
    return PlanOutput(
        plan_id=stored.plan_id,
        channel=plan.channel,
        ladder_version=plan.ladder_version,
        start_ms=plan.start_ms,
        start=store.iso(plan.start_ms),
        end_ms=plan.end_ms,
        end=store.iso(plan.end_ms),
        programs=rows,
        violations=plan.violations,
        publishable=plan.ok,
        expires_at=datetime.fromtimestamp(stored.expires_at, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


@tool("schedule", access=Access.WRITE)
def plan(payload: PlanInput, ctx: ToolContext) -> PlanOutput:
    """Plan a block of programs on a channel; nothing changes on air. The picks air back to back in the given
    order from start, right after the program named in after, or by default where the published schedule ends
    (10 minutes from now when it is empty). Computes every start time, on-air duration (content plus ad breaks),
    ad break and SCTE-35 id, and checks the brief (ratings, genres, dayparts) and the schedule (overlaps, lead
    time). Returns a plan_id with the program table in UTC, the violations and publishable. Error violations block
    publishing: change the picks or the start and plan again; warnings are for the operator to accept. Publish
    with schedule___publish within the hour."""
    channel = require_channel(payload.channel)
    at = now_ms()
    ladder = store.ladder_definition(channel.ladder_version)
    upcoming = recent_programs(channel.name, at - GC_WINDOW_MS)
    start = _block_start(payload, [p for p in upcoming if p.end_ms > at], at)
    assets, metadata, problems = resolve_picks(payload.picks, channel.ladder_version)
    brief = channel.brief or ProgrammingBrief()
    if payload.break_policy is not None:
        brief = brief.model_copy(update={"break_policy": payload.break_policy})
    planned = plan_block(
        channel=channel.name,
        ladder_version=channel.ladder_version,
        grid=ladder.grid,
        start_ms=start,
        assets=assets,
        brief=brief,
        now_ms=at,
        existing=upcoming,
    )
    planned = planned.model_copy(update={"violations": [*problems, *planned.violations]})
    live = live_programs(channel.name, known_starts={p.name: p.start_ms for p in upcoming})
    stored = StoredPlan(
        plan_id=new_plan_id(channel.name),
        plan=planned,
        metadata=metadata,
        fingerprint=anchor(live, start).fingerprint,
        correlation_id=ctx.correlation_id,
        expires_at=int(time.time()) + PLAN_TTL_S,
    )
    stored.save()
    logger.info("plan stored", extra={"plan_id": stored.plan_id, "programs": len(planned.programs)})
    return plan_output(stored)


# --- publish ---------------------------------------------------------------------------------------------------------


class PublishInput(BaseModel):
    plan_id: str = Field(description="plan_id returned by schedule___plan in this conversation.", min_length=3)


class PublishOutput(epg.Epg):
    """The published block as guide entries (CONTRACTS §I), with Channel Assembly alerts after the change."""

    plan_id: str
    published: int = Field(description="Number of programs now scheduled from this plan.")
    start_ms: int
    start: str
    end_ms: int
    end: str
    alerts: list[Alert] = Field(description="Channel alerts after publishing (harmless codes filtered out).")


def _stale(message: str) -> ToolError:
    return ToolError("PLAN_STALE", message, hint="Call schedule___get_schedule and plan again.")


def _revalidate(stored: StoredPlan, upcoming: Sequence[store.ProgramRecord], at_ms: int) -> None:
    plan = stored.plan
    if not plan.ok:
        raise ToolError("VALIDATION", "the plan has error violations", hint="Fix the picks and plan again.")
    names = {p.name for p in plan.programs}
    others = [p for p in upcoming if p.name not in names]
    violations = validate_plan(plan, now_ms=at_ms, existing=others)
    if errors := [f"{v.code}: {v.message}" for v in violations if v.severity == "error"]:
        raise _stale("; ".join(errors))
    if unready := _unready(plan):
        raise _stale("; ".join(unready))


def _unready(plan: Plan) -> list[str]:
    """Assets of ``plan`` that are no longer READY with the VOD source they were planned with (CreateProgram would
    fail part-way through the block)."""
    problems = []
    for asset_id, vod_source in dict.fromkeys((p.asset_id, p.vod_source) for p in plan.programs):
        record = store.get_asset(asset_id, plan.ladder_version)
        if record is None or record.status is not store.AssetStatus.READY or record.vod_source != vod_source:
            state = f"{record.status} with VOD source {record.vod_source}" if record else "gone from the library"
            problems.append(f"NOT_READY: {asset_id} is {state}, planned with {vod_source}")
    return problems


def _requests(
    stored: StoredPlan, *, slates: Mapping[int, str], source_location: str, position: Anchor, live: Iterable[str]
) -> list[dict[str, Any]]:
    """CreateProgram requests for the programs not yet in the ``live`` schedule (a retried publish resumes).

    The first program is ABSOLUTE and the rest RELATIVE (``core.specs.program_requests``). Channel Assembly checks
    RelativeProgram before duplicate names, so existing programs are skipped rather than re-created (verified live).
    """
    requests = specs.program_requests(
        stored.plan, source_location=source_location, slate_vod_sources=slates, after_program=position.after
    )
    if requests and position.before is not None:
        # Nothing airs before the block: it goes in front of the first program of a non-empty schedule.
        requests[0]["ScheduleConfiguration"]["Transition"] |= {
            "RelativePosition": "BEFORE_PROGRAM",
            "RelativeProgram": position.before,
        }
    for request, program in zip(requests, stored.plan.programs, strict=True):
        request["SourceLocationName"] = stored.metadata[program.asset_id].source_location or source_location
    present = set(live)
    return [r for r in requests if r["ProgramName"] not in present]


def _slates(ladder_version: str, plan: Plan) -> dict[int, str]:
    record = store.get_ladder(ladder_version)
    slates = record.kit.break_slates if record is not None else {}
    if missing := sorted({b.slate_gops for p in plan.programs for b in p.breaks} - slates.keys()):
        raise ToolError(
            "NOT_FOUND",
            f"ladder {ladder_version} has no break slate of {missing} GOPs",
            hint="Use a break policy whose slate_gops the ladder kit provides (encoding___get_ladder).",
        )
    return dict(slates)


def _published(stored: StoredPlan) -> list[store.ProgramRecord]:
    """The plan's programs as PUBLISHED guide records."""
    return [
        store.ProgramRecord(
            **program.model_dump(),
            channel=stored.channel,
            status=store.ProgramStatus.PUBLISHED,
            plan_id=stored.plan_id,
            correlation_id=stored.correlation_id,
            **stored.metadata[program.asset_id].model_dump(),
        )
        for program in stored.plan.programs
    ]


@dataclass(frozen=True, slots=True)
class Committed:
    """The programs of a published plan as recorded, and the names this call created in Channel Assembly."""

    records: list[store.ProgramRecord]
    created: list[str]


def commit(
    stored: StoredPlan, requests: Sequence[Mapping[str, Any]], *, present: Iterable[str], ctx: ToolContext
) -> Committed:
    """Create the missing programs, recording each in the media library as soon as Channel Assembly has it.

    The library therefore lists every program that airs even when a call fails part-way, the function runs out of
    time or it is killed (at most the program being created is unrecorded). A failure raises a ToolError that says
    how far the block got and how to resume (:data:`RESUME_HINT`).
    """
    planned = {record.name: record for record in _published(stored)}
    live = set(present)
    records = [store.put(record) for name, record in planned.items() if name in live]  # from an earlier attempt
    created: list[str] = []
    try:
        for name, new in create_programs(requests):
            records.append(store.put(planned[name]))
            if new:
                created.append(name)
            if len(records) < len(planned) and ctx.out_of_time(PUBLISH_TIME_MARGIN_MS):
                raise ToolError("UPSTREAM", "the function is running out of time", retryable=True)
    except Exception as error:
        logger.exception("publish stopped part-way", extra={"recorded": len(records), "planned": len(planned)})
        cause = dispatch.as_tool_error(error)
        raise ToolError(
            cause.code,
            f"published {len(records)} of {len(planned)} programs of {stored.plan_id}, then: {cause.message}",
            retryable=cause.retryable,
            hint=RESUME_HINT,
        ) from error
    return Committed(records=records, created=created)


def _refresh_guide(channel: store.ChannelRecord, at_ms: int) -> None:
    """Rewrite the guide after a partial publish without hiding the failure being reported."""
    try:
        write_guide(channel, at_ms)
    except ClientError, BotoCoreError:
        logger.exception("guide not rewritten after a partial publish")


def _emit(event: SchedulePublished) -> None:
    """Announce a publish; it has happened, so a failure to announce it is only logged."""
    config = settings()
    try:
        response = aws.client("events").put_events(
            Entries=[put_events_entry(event, prefix=config.prefix, event_bus=config.event_bus)]
        )
    except ClientError, BotoCoreError:
        logger.exception("SchedulePublished event not sent")
        return
    if response.get("FailedEntryCount"):
        logger.error("SchedulePublished event rejected", extra={"entries": response.get("Entries")})


@tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
def publish(payload: PublishInput, ctx: ToolContext) -> PublishOutput:
    """Publish a plan to the channel's live schedule; this changes what airs. Takes only the plan_id of a
    publishable plan from schedule___plan (plans expire after an hour). Fails with PLAN_STALE when the schedule
    changed since planning: call schedule___get_schedule and plan again. Returns the published programs as guide
    entries and the channel's alerts."""
    stored = StoredPlan.load(payload.plan_id)
    channel = require_channel(stored.channel)
    config = settings()
    config.require("source_location")
    with lease(channel.name, stored.plan_id):
        at = now_ms()
        upcoming = recent_programs(channel.name, at - GC_WINDOW_MS)
        _revalidate(stored, upcoming, at)
        names = [p.name for p in stored.plan.programs]
        live = live_programs(channel.name, known_starts={p.name: p.start_ms for p in upcoming})
        position = anchor(live, stored.plan.start_ms, exclude=names)
        if position.fingerprint != stored.fingerprint:
            raise _stale(f"the schedule of {channel.name} changed since the plan was made")
        requests = _requests(
            stored,
            slates=_slates(channel.ladder_version, stored.plan),
            source_location=config.source_location,
            position=position,
            live=(p.name for p in live),
        )
        try:
            committed = commit(stored, requests, present=(p.name for p in live), ctx=ctx)
        except ToolError:
            _refresh_guide(channel, at)
            raise
        write_guide(channel, at)
    plan = stored.plan
    _emit(
        SchedulePublished(
            channel=channel.name,
            plan_id=stored.plan_id,
            programs=len(committed.records),
            start_ms=plan.start_ms,
            end_ms=plan.end_ms,
            correlation_id=ctx.correlation_id,
        )
    )
    logger.info(
        "plan published",
        extra={
            "plan_id": stored.plan_id,
            "after": position.after,
            "before": position.before,
            "programs_created": committed.created,
        },
    )
    return PublishOutput(
        channel=channel.name,
        generated_at=store.iso(at),
        ladder_version=channel.ladder_version,
        programs=[guide_entry(r) for r in committed.records],
        plan_id=stored.plan_id,
        published=len(committed.records),
        start_ms=plan.start_ms,
        start=store.iso(plan.start_ms),
        end_ms=plan.end_ms,
        end=store.iso(plan.end_ms),
        alerts=after_change(lambda: channel_arn(channel.name, channel.arn)),
    )


# --- remove_programs -------------------------------------------------------------------------------------------------


class RemoveProgramsInput(BaseModel):
    channel: str = Field(description="Channel name from channels___list_channels.", min_length=1, max_length=64)
    programs: list[str] = Field(
        min_length=1,
        max_length=MAX_PICKS,
        description="Names of upcoming programs to remove, from schedule___get_schedule.",
    )


class RemoveProgramsOutput(BaseModel):
    channel: str
    removed: list[str]
    not_found: list[str] = Field(description="Names that are not programs of the channel starting in the future.")
    moved: list[epg.EpgProgram] = Field(description="Later programs of the same blocks, now airing earlier.")


def reflow(programs: Sequence[store.ProgramRecord], removed: set[str]) -> list[store.ProgramRecord]:
    """The programs left after removing ``removed``, re-timed the way Channel Assembly re-links its schedule.

    A block's first program is ABSOLUTE and keeps its start; the rest of a block is RELATIVE and follows its
    predecessor in air order (deleting a program re-links its successor, verified live).
    """
    first_of_block: dict[str | None, str] = {}
    for p in programs:
        first_of_block.setdefault(p.plan_id, p.name)
    kept: list[store.ProgramRecord] = []
    for p in programs:
        if p.name in removed:
            continue
        relative = first_of_block[p.plan_id] != p.name and p.plan_id is not None and bool(kept)
        kept.append(p.model_copy(update={"start_ms": kept[-1].end_ms}) if relative else p)
    return kept


@tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
def remove_programs(payload: RemoveProgramsInput, ctx: ToolContext) -> RemoveProgramsOutput:
    """Remove programs that have not started yet from a channel's live schedule and guide. Later programs of the
    same block move up to close the hole; the next block keeps its start, so filler airs in between. Programs that
    are on air or over are reported in not_found."""
    channel = require_channel(payload.channel)
    with lease(channel.name, f"remove.{ctx.correlation_id}"):
        at = now_ms()
        upcoming = recent_programs(channel.name, at - GC_WINDOW_MS)
        future = {p.name for p in upcoming if p.start_ms > at}
        wanted = list(dict.fromkeys(payload.programs))
        removed = delete_programs(channel.name, [name for name in wanted if name in future])
        before = {p.name: p for p in upcoming}
        moved = [p for p in reflow(upcoming, set(removed)) if p.start_ms != before[p.name].start_ms]
        for name in [*removed, *(p.name for p in moved)]:
            store.delete_item(before[name].key())
        for program in moved:
            store.put(program)
        write_guide(channel, at)
    logger.info("programs removed", extra={"removed": removed, "moved": [p.name for p in moved]})
    return RemoveProgramsOutput(
        channel=channel.name,
        removed=removed,
        not_found=[n for n in wanted if n not in future],
        moved=[guide_entry(p) for p in moved],
    )


# --- briefs ----------------------------------------------------------------------------------------------------------


@tool("schedule", access=Access.READ)
def get_brief(payload: ChannelInput, ctx: ToolContext) -> BriefOutput:
    """Get a channel's programming brief: allowed ratings and genres, dayparts (UTC) and the ad-break policy that
    every plan is checked against. Read it before picking content."""
    channel = require_channel(payload.channel)
    return BriefOutput(channel=channel.name, brief=channel.brief or ProgrammingBrief(), default=channel.brief is None)


class UpdateBriefInput(BaseModel):
    channel: str = Field(description="Channel name from channels___list_channels.", min_length=1, max_length=64)
    brief: ProgrammingBrief = Field(description="The complete new brief; it replaces the current one.")


@tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
def update_brief(payload: UpdateBriefInput, ctx: ToolContext) -> BriefOutput:
    """Replace a channel's programming brief, the standing rules every future plan is checked against. Pass the
    complete brief: start from schedule___get_brief and change only what the operator asked for."""
    channel = require_channel(payload.channel)
    updated = channel.model_copy(update={"brief": payload.brief, "correlation_id": ctx.correlation_id})
    store.put(updated, condition=Attr("pk").exists())
    logger.info("brief updated", extra={"brief": json.loads(payload.brief.model_dump_json())})
    return BriefOutput(channel=channel.name, brief=payload.brief, default=False)


handler = dispatch.handler("schedule")
