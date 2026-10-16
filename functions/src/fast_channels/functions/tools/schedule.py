"""``schedule`` tools: read a channel's guide, plan a block, publish it to Channel Assembly, edit the brief.

The MediaLibrary ``PROGRAM`` items are the schedule of record (Channel Assembly deletes aired programs about 15
minutes after they finish, channel-assembly.md §5.6). Planning is pure arithmetic in ``core.timeline.plan_block``;
publishing replays a stored plan against the live schedule:

* one writer per channel: a conditional ``LEASE`` item with a TTL (concurrent edits raise ConflictException);
* re-anchoring: the block is linked after the live program that precedes it *in time*, because list order beats
  wall-clock time for ABSOLUTE programs (channel-assembly.md §5.3);
* ``PLAN_STALE`` when the live schedule from the anchor on changed since planning (fingerprint of that tail);
* CreateProgram strictly sequential at ≤ 3 TPS; deterministic program names make a retried publish idempotent.
"""

import hashlib
import json
import math
import secrets
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, Final, Self

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError, model_validator

from fast_channels.core import specs
from fast_channels.core.catalog import ContentItem
from fast_channels.core.events import SchedulePublished, put_events_entry
from fast_channels.core.ingest import AssetKind
from fast_channels.core.timeline import (
    LEAD_TIME_MS,
    BreakPolicy,
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
from fast_channels.functions.custom_resources.channel_lifecycle import SCHEDULE_MINUTES
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _library_store as store
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, tool

logger = Logger()

PLAN_TTL_S: Final = 3600
LEASE_TTL_S: Final = 360
"""Longer than the schedule function's 5-minute timeout, so a crashed publish frees the channel by itself."""
CREATE_PROGRAM_INTERVAL_S: Final = 0.34  # CreateProgram and DeleteProgram: 3 TPS (channel-assembly.md §5.6)
CONFLICT_RETRIES: Final = 4
GC_WINDOW_MS: Final = 15 * 60_000  # aired programs linger in Channel Assembly ~15 min (channel-assembly.md §5.6)
EPG_HISTORY_MS: Final = 6 * 3600_000
MAX_PICKS: Final = 100
NOISE_ALERTS: Final = frozenset({"SEGMENT_DURATION_MISMATCH", "BANDWIDTH_MISMATCH"})
"""Harmless with GOP-snapped ESAM splits and normalised BANDWIDTH; they hide real alerts (_review.md, e2e.md §8)."""
MAX_ALERTS: Final = 20


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _epoch_ms(moment: datetime) -> int:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return round(aware.timestamp() * 1000)


# --- shared models ---------------------------------------------------------------------------------------------------


class ChannelInput(BaseModel):
    channel: str = Field(description="Channel name, e.g. fast-demo.", min_length=1, max_length=64)


class Gap(BaseModel):
    """Filler time between two published programs."""

    start_ms: int
    start: str
    end_ms: int
    end: str
    duration_ms: int


class Alert(BaseModel):
    """A Channel Assembly alert (ListAlerts), noise codes removed."""

    code: str
    category: str
    message: str
    resource_arn: str


class BriefOutput(BaseModel):
    channel: str
    brief: ProgrammingBrief
    default: bool = Field(description="True when the channel has no brief yet and the defaults apply.")


# --- live schedule (Channel Assembly) --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LiveProgram:
    """A program in Channel Assembly's schedule, in list (= air) order."""

    name: str
    start_ms: int
    """The planned start from the MediaLibrary when known, else Channel Assembly's approximate start."""


def live_programs(channel: str, *, known_starts: Mapping[str, int] | None = None) -> list[LiveProgram]:
    """Programs of ``channel`` in schedule order (filler entries skipped).

    Channel Assembly only approximates starts (a RELATIVE program created on a stopped channel reports 1970, and
    sources it cannot read report zero durations; verified live), so the MediaLibrary's planned starts win.
    """
    known = known_starts or {}
    paginator = aws.client("mediatailor").get_paginator("get_channel_schedule")
    entries = paginator.paginate(ChannelName=channel, DurationMinutes=str(SCHEDULE_MINUTES))
    return [
        LiveProgram(entry["ProgramName"], known.get(entry["ProgramName"], _epoch_ms(entry["ApproximateStartTime"])))
        for page in entries
        for entry in page.get("Items", [])
        if entry.get("ScheduleEntryType") == "PROGRAM"
    ]


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


@dataclass(slots=True)
class Pacer:
    """Spaces calls at least ``interval_s`` apart (MediaTailor per-API TPS quotas)."""

    interval_s: float
    _last: float = field(default=-math.inf)

    def wait(self) -> None:
        if (delay := self._last + self.interval_s - time.monotonic()) > 0:
            time.sleep(delay)
        self._last = time.monotonic()


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _already_exists(error: ClientError) -> bool:
    # A duplicate ProgramName is BadRequestException "This program already exists." (verified live).
    message = str(error.response.get("Error", {}).get("Message", ""))
    return _error_code(error) in {"BadRequestException", "ConflictException"} and "already exists" in message


def create_programs(requests: Sequence[Mapping[str, Any]], pacer: Pacer) -> list[str]:
    """CreateProgram for each request in order; returns the names that already existed (idempotent retry).

    ConflictException "The channel schedule has changed" is retried with backoff (channel-assembly.md §5.2).
    """
    mediatailor, existing = aws.client("mediatailor"), []
    for request in requests:
        for attempt in range(CONFLICT_RETRIES + 1):
            pacer.wait()
            try:
                mediatailor.create_program(**request)
                break
            except ClientError as error:
                if _already_exists(error):
                    existing.append(request["ProgramName"])
                    break
                if _error_code(error) != "ConflictException" or attempt == CONFLICT_RETRIES:
                    raise
                time.sleep(2**attempt)
    return existing


def delete_programs(channel: str, names: Iterable[str], pacer: Pacer) -> None:
    """DeleteProgram for each name (deleting a missing program succeeds, channel-assembly.md §5.1)."""
    mediatailor = aws.client("mediatailor")
    for name in names:
        pacer.wait()
        mediatailor.delete_program(ChannelName=channel, ProgramName=name)


def channel_arn(record: store.ChannelRecord) -> str:
    return record.arn or str(aws.client("mediatailor").describe_channel(ChannelName=record.name)["Arn"])


def alerts(resource_arn: str) -> list[Alert]:
    """ListAlerts of a resource without the noise codes, at most :data:`MAX_ALERTS`."""
    pages = aws.client("mediatailor").get_paginator("list_alerts").paginate(ResourceArn=resource_arn)
    found = [
        Alert(
            code=item["AlertCode"],
            category=item.get("Category", ""),
            message=item.get("AlertMessage", "")[:300],
            resource_arn=item.get("ResourceArn", resource_arn),
        )
        for page in pages
        for item in page.get("Items", [])
        if not any(code in f"{item['AlertCode']} {item.get('AlertMessage', '')}" for code in NOISE_ALERTS)
    ]
    return found[:MAX_ALERTS]


# --- MediaLibrary helpers --------------------------------------------------------------------------------------------


def require_channel(name: str) -> store.ChannelRecord:
    record = store.get_channel(name)
    if record is None:
        raise ToolError("NOT_FOUND", f"channel {name} does not exist", hint="Call channels___list_channels.")
    return record


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
        if _error_code(error) != "ConditionalCheckFailedException":
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
            if _error_code(error) != "ConditionalCheckFailedException":
                raise


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
    """Programs that end after ``at_ms`` (anything starting more than a day earlier has long finished)."""
    return [p for p in store.programs(channel, start_ms=max(0, at_ms - 86_400_000)) if p.end_ms > at_ms]


def write_guide(channel: store.ChannelRecord, at_ms: int) -> epg.Epg:
    """Render and upload ``epg/<channel>.{json,xml}`` from the MediaLibrary."""
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


# --- get_schedule ----------------------------------------------------------------------------------------------------


class ScheduleOutput(epg.Epg):
    """The channel guide (CONTRACTS §I) from now on, plus where the published schedule ends and its gaps."""

    end_ms: int | None = Field(description="End of the last published program; null when nothing is scheduled.")
    end: str | None
    gaps: list[Gap] = Field(description="Filler slate between published programs.")


@tool("schedule", access=Access.READ)
def get_schedule(payload: ChannelInput, ctx: ToolContext) -> ScheduleOutput:
    """Get a channel's published schedule from now on: programs with UTC start/end, title, rating and breaks,
    the end of the schedule (plan from there to extend it) and the filler gaps between programs."""
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
    channel: str = Field(description="Channel name.", min_length=1, max_length=64)
    picks: list[str] = Field(
        min_length=1,
        max_length=MAX_PICKS,
        description="Asset ids in air order (from library___find_assets, READY on the channel's ladder version).",
    )
    start: datetime | None = Field(
        None, description="UTC ISO-8601 start of the block. Omit start and after to append to the schedule."
    )
    after: str | None = Field(None, description="Name of a published program the block starts right after.")
    break_policy: BreakPolicy | None = Field(None, description="Overrides the brief's ad-break policy for this block.")

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
    """Plan a block of programs: the ordered picks start at `start`, right after the program `after`, or (by
    default) at the end of the published schedule. Computes every start time, on-air duration, ad break and
    SCTE-35 id, checks the brief (ratings, genres, dayparts) and the schedule, and returns a plan_id with the
    program table and violations. Errors block publishing; warnings do not. Nothing changes on air."""
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
    errors = [v for v in validate_plan(plan, now_ms=at_ms, existing=others) if v.severity == "error"]
    if errors:
        raise _stale("; ".join(f"{v.code}: {v.message}" for v in errors))


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


def _emit(event: SchedulePublished) -> None:
    config = settings()
    response = aws.client("events").put_events(
        Entries=[put_events_entry(event, prefix=config.prefix, event_bus=config.event_bus)]
    )
    if response.get("FailedEntryCount"):
        logger.error("SchedulePublished event rejected", extra={"entries": response.get("Entries")})


@tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
def publish(payload: PublishInput, ctx: ToolContext) -> PublishOutput:
    """Publish a plan to the channel's live schedule (changes what airs). Takes only the plan_id from
    schedule___plan; fails with PLAN_STALE when the schedule changed since planning, then plan again."""
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
        existed = create_programs(requests, Pacer(CREATE_PROGRAM_INTERVAL_S))
        records = [store.put(r) for r in _published(stored)]
        write_guide(channel, at)
    plan = stored.plan
    _emit(
        SchedulePublished(
            channel=channel.name,
            plan_id=stored.plan_id,
            programs=len(records),
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
            "programs_created": len(requests) - len(existed),
            "already_existed": existed,
        },
    )
    return PublishOutput(
        channel=channel.name,
        generated_at=store.iso(at),
        ladder_version=channel.ladder_version,
        programs=[guide_entry(r) for r in records],
        plan_id=stored.plan_id,
        published=len(records),
        start_ms=plan.start_ms,
        start=store.iso(plan.start_ms),
        end_ms=plan.end_ms,
        end=store.iso(plan.end_ms),
        alerts=alerts(channel_arn(channel)),
    )


# --- remove_programs -------------------------------------------------------------------------------------------------


class RemoveProgramsInput(BaseModel):
    channel: str = Field(description="Channel name.", min_length=1, max_length=64)
    programs: list[str] = Field(min_length=1, max_length=MAX_PICKS, description="Program names to remove.")


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
    same block move up to close the hole; the next block keeps its start (filler slate airs in between)."""
    channel = require_channel(payload.channel)
    with lease(channel.name, f"remove.{ctx.correlation_id}"):
        at = now_ms()
        upcoming = recent_programs(channel.name, at - GC_WINDOW_MS)
        future = {p.name for p in upcoming if p.start_ms > at}
        wanted = list(dict.fromkeys(payload.programs))
        removed = [name for name in wanted if name in future]
        delete_programs(channel.name, removed, Pacer(CREATE_PROGRAM_INTERVAL_S))
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
    """Get a channel's programming brief: allowed ratings and genres, dayparts (UTC) and the ad-break policy."""
    channel = require_channel(payload.channel)
    return BriefOutput(channel=channel.name, brief=channel.brief or ProgrammingBrief(), default=channel.brief is None)


class UpdateBriefInput(BaseModel):
    channel: str = Field(description="Channel name.", min_length=1, max_length=64)
    brief: ProgrammingBrief = Field(description="The complete new brief; it replaces the current one.")


@tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
def update_brief(payload: UpdateBriefInput, ctx: ToolContext) -> BriefOutput:
    """Replace a channel's programming brief (the standing rules every future plan is checked against)."""
    channel = require_channel(payload.channel)
    updated = channel.model_copy(update={"brief": payload.brief, "correlation_id": ctx.correlation_id})
    store.put(updated, condition=Attr("pk").exists())
    logger.info("brief updated", extra={"brief": json.loads(payload.brief.model_dump_json())})
    return BriefOutput(channel=channel.name, brief=payload.brief, default=False)


handler = dispatch.handler("schedule")
