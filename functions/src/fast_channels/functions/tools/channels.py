"""``channels`` tools: list, inspect, start and stop linear channels; create and delete agent-owned ones.

Channels defined in ``cdk.json`` are owned by CloudFormation (``owner = iac``); the agent may start and stop them but
creates and deletes only its own ``<prefix>-agent-*`` channels (the Gateway's Cedar policy enforces the same rule).
An agent channel is assembled from the same ``core.specs`` builders the CDK constructs use: a LINEAR channel with
the ladder kit's filler slate, AS_RUN logs, a per-channel PLAYER_SELECT playback configuration with its three
vended-log deliveries, and a SigV4-only channel policy for exactly that configuration (e2e.md §2.7-2.11).

A started channel bills by the hour, so ``start_channel`` first creates a one-time EventBridge Scheduler schedule that
stops it after ``channels.max_runtime_hours`` (verified: the universal target ``mediatailor:stopChannel`` stops the
channel and the schedule deletes itself), then starts it: a channel never runs without a pending stop, and a stop
that fires on a channel that never started is harmless.

Before a stopped channel starts, ``start_channel`` removes the programs whose planned start has passed
(:func:`skip_missed`): Channel Assembly plays a stopped channel's schedule from its head, so it would air them late
and replay aired programs it has not deleted yet. Every program is ABSOLUTE (``core.specs.program_requests``), so
the rest of the schedule airs at its planned times.
"""

import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from fast_channels.core import specs
from fast_channels.core.naming import agent_owned_name, is_agent_owned, mediatailor_name
from fast_channels.core.profiles import applicable, profile
from fast_channels.core.tags import resource_tags
from fast_channels.core.timeline import ProgrammingBrief  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.functions import aws, channel_ops, epg
from fast_channels.functions.channel_ops import ChannelStateTimeoutError, is_missing
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools._alerts import Alert, after_change
from fast_channels.functions.tools._guide import PROGRAM_LOOKBACK_MS, guide_entry, write_guide
from fast_channels.functions.tools._lease import lease
from fast_channels.functions.tools.library_store import ChannelInput, definition, require_channel
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, tool

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

logger = Logger()

STOP_CHANNEL_TARGET: Final = "arn:aws:scheduler:::aws-sdk:mediatailor:stopChannel"
"""EventBridge Scheduler universal target; the schedule's Input is the StopChannel request."""
START_TIME_MARGIN_MS: Final = 150_000
"""``start_channel`` stops removing missed programs this long before the function times out, keeping the time to
start the channel and wait until it runs (``channel_ops.STATE_TIMEOUT_S``)."""
DRAIN_TIME_MARGIN_MS: Final = 60_000
"""``delete_channel`` stops deleting programs this long before the function times out, keeping the time to delete
the playback configuration, its log deliveries and the channel once the drain is complete."""

PRIMARY_CDN: Final = "cloudfront"
"""Name of the deployment's own CloudFront distribution (``CDN_BASE_URL``) in per-CDN playback listings."""

type ChannelState = Literal["RUNNING", "STOPPED", "MISSING"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ignore_missing(operation: Callable[..., Any], **request: Any) -> None:
    try:
        operation(**request)
    except ClientError as error:
        if not is_missing(error):
            raise


def _tags(ctx: ToolContext, ladder_version: str) -> dict[str, str]:
    """CONTRACTS §E tags of an agent-created resource (the reaper deletes by ``fast:app``)."""
    return resource_tags(
        settings().app, store.Owner.AGENT, ladder_version=ladder_version, correlation_id=ctx.correlation_id
    )


def _in_transition(error: ChannelStateTimeoutError, channel: str) -> ToolError:
    return ToolError(
        "UPSTREAM",
        str(error),
        retryable=True,
        hint=f"The change is under way: check {channel} with channels___get_channel in a minute.",
    )


# --- models ----------------------------------------------------------------------------------------------------------


type ChannelOwner = Literal[store.Owner.IAC, store.Owner.AGENT]
type Playback = dict[str, dict[str, str]]
"""Viewer URLs per channel output: ``ssai`` and ``sgai``."""


class ChannelSummary(BaseModel):
    name: str
    state: ChannelState = Field(
        description="RUNNING (viewers can watch; billed by the hour), STOPPED, or MISSING (in the media library "
        "but not in Channel Assembly)."
    )
    owner: ChannelOwner = Field(description="iac: defined by the deployment; agent: created with channels tools.")
    ladder_version: str = Field(description="The only ladder version whose assets this channel can air.")
    outputs: list[str] = Field(description="Outputs (rendition profiles), e.g. hls.")
    tier: str | None = Field(description="BASIC (~$0.10/h while running) or STANDARD (~$0.35/h).")


class ListChannelsInput(BaseModel):
    owner: ChannelOwner | None = Field(None, description="Only channels of this owner.")


class ListChannelsOutput(BaseModel):
    channels: list[ChannelSummary]


class CdnPlayback(BaseModel):
    """The viewer URLs of a channel through one CDN."""

    cdn: str = Field(description=f"CDN name; {PRIMARY_CDN} is the deployment's own distribution.")
    playback: Playback


class ChannelDetail(ChannelSummary):
    arn: str | None
    playback_configuration: str | None = Field(description="MediaTailor playback configuration (ad insertion).")
    playback: Playback = Field(
        description="Viewer URLs per output through the primary CDN: ssai and sgai (HLS Interstitials). Empty when "
        "the channel has no playback configuration."
    )
    cdn_playback: list[CdnPlayback] | None = Field(
        None,
        description="The same URLs through every configured CDN, primary first; only when the deployment lists "
        "other CDNs.",
    )
    auto_stop_at: str | None = Field(description="UTC ISO-8601 time the pending auto-stop fires; null when none.")


class StateChange(BaseModel):
    channel: str
    state: Literal["RUNNING", "STOPPED"]
    auto_stop_at: str | None = Field(None, description="UTC ISO-8601 time the channel is stopped automatically.")
    alerts: list[Alert] = Field([], description="Channel alerts after the change (expected warnings left out).")


class SkippedProgram(BaseModel):
    """A program removed unaired because the channel started after its planned start."""

    name: str
    title: str
    start: str = Field(description="Planned UTC ISO-8601 start.")
    end: str = Field(description="Planned UTC ISO-8601 end.")


class StartChannelOutput(StateChange):
    skipped: list[SkippedProgram] | None = Field(
        None,
        description="Programs whose planned start had passed when the stopped channel started: removed unaired, "
        "SKIPPED in the guide. Absent when none.",
    )
    hint: str | None = Field(None, description="What to tell the operator about the skipped programs.")


class CreateChannelInput(BaseModel):
    name: str = Field(
        pattern=r"^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$",
        description="Short name, e.g. kids; the channel is named <prefix>-agent-<name>.",
    )
    ladder_version: str | None = Field(
        None, description="READY ladder version (encoding___list_ladders); omit for the deployment default."
    )
    outputs: list[str] | None = Field(
        None,
        description="Outputs (rendition profiles) with renditions on the ladder, at most 5; omit for every package "
        "profile of the deployment.",
    )
    tier: Literal["BASIC", "STANDARD"] = Field("BASIC", description="BASIC ~$0.10/h, STANDARD ~$0.35/h while running.")
    brief: ProgrammingBrief | None = Field(
        None, description="Programming brief every plan is checked against; omit for no rules (default break policy)."
    )


class DeleteChannelOutput(BaseModel):
    channel: str
    state: Literal["DELETED"] = "DELETED"
    programs_deleted: int


# --- reads -----------------------------------------------------------------------------------------------------------


def _live_states() -> dict[str, str]:
    pages = aws.client("mediatailor").get_paginator("list_channels").paginate()
    return {c["ChannelName"]: c["ChannelState"] for page in pages for c in page.get("Items", [])}


def _summary(record: store.ChannelRecord, state: str | None) -> dict[str, Any]:
    return {
        "name": record.name,
        "state": state or "MISSING",
        "owner": record.owner,
        "ladder_version": record.ladder_version,
        "outputs": record.outputs,
        "tier": record.tier,
    }


@tool("channels", access=Access.READ)
def list_channels(payload: ListChannelsInput, ctx: ToolContext) -> ListChannelsOutput:
    """List the deployment's channels: live state (RUNNING is billed by the hour), owner, tier, ladder version and
    outputs. channels___get_channel adds the playback URLs and the auto-stop time."""
    states = _live_states()
    return ListChannelsOutput(
        channels=[ChannelSummary(**_summary(r, states.get(r.name))) for r in store.list_channels(payload.owner)]
    )


def _hls_prefix(record: store.ChannelRecord) -> str | None:
    if record.hls_prefix:
        return record.hls_prefix
    if not record.playback_configuration_name:
        return None
    try:
        config = aws.client("mediatailor").get_playback_configuration(Name=record.playback_configuration_name)
    except ClientError as error:
        if is_missing(error):
            return None
        raise
    return str(config["HlsConfiguration"]["ManifestEndpointPrefix"])


def playback_by_cdn(record: store.ChannelRecord) -> dict[str, Playback]:
    """Viewer URLs (``core.specs.playback_urls``) per CDN, :data:`PRIMARY_CDN` first, then ``CDN_ENDPOINTS``.

    Other CDNs chain to CloudFront and serve the same paths under their ``manifest_base``. Empty without a playback
    configuration or a primary CDN.
    """
    config = settings()
    hls = _hls_prefix(record)
    if hls is None or not config.cdn_base_url:
        return {}
    bases = {PRIMARY_CDN: config.cdn_base_url}
    for endpoint in config.cdn_endpoints:
        bases.setdefault(endpoint.name, endpoint.manifest_base)
    return {
        cdn: specs.playback_urls(cdn_base=base, playback_prefix_hls=hls, outputs=record.outputs)
        for cdn, base in bases.items()
    }


def _auto_stop_name(channel: str) -> str:
    return mediatailor_name(channel, "auto-stop")  # Scheduler names: [0-9a-zA-Z-_.]{1,64}


def auto_stop_at(channel: str) -> str | None:
    """When the pending auto-stop schedule of ``channel`` fires (UTC ISO-8601), if there is one."""
    group = settings().scheduler_group
    if not group:
        return None
    try:
        schedule = aws.client("scheduler").get_schedule(Name=_auto_stop_name(channel), GroupName=group)
    except ClientError as error:
        if is_missing(error):
            return None
        raise
    match = re.fullmatch(r"at\((.+)\)", schedule.get("ScheduleExpression", ""))
    return f"{match.group(1)}Z" if match else None


def _describe(name: str) -> dict[str, Any] | None:
    try:
        return dict(aws.client("mediatailor").describe_channel(ChannelName=name))
    except ClientError as error:
        if is_missing(error):
            return None
        raise


def _detail(record: store.ChannelRecord, live: dict[str, Any] | None) -> ChannelDetail:
    urls = playback_by_cdn(record)
    return ChannelDetail(
        **_summary(record, live["ChannelState"] if live else None),
        arn=live["Arn"] if live else record.arn,
        playback_configuration=record.playback_configuration_name,
        playback=urls.get(PRIMARY_CDN, {}),
        cdn_playback=[CdnPlayback(cdn=cdn, playback=p) for cdn, p in urls.items()] if len(urls) > 1 else None,
        auto_stop_at=auto_stop_at(record.name),
    )


@tool("channels", access=Access.READ)
def get_channel(payload: ChannelInput, ctx: ToolContext) -> ChannelDetail:
    """Get one channel: live state, owner, tier, ladder version, outputs, the viewer playback URLs per output
    (ssai and sgai; per CDN when several are configured) and the pending auto-stop time. Give operators these URLs
    verbatim; never build playback URLs yourself."""
    record = require_channel(payload.channel)
    return _detail(record, _describe(record.name))


# --- start / stop ----------------------------------------------------------------------------------------------------


def schedule_auto_stop(channel: str, *, at: datetime, ctx: ToolContext) -> None:
    """Create (or move) the one-time schedule that stops ``channel`` at ``at``; it deletes itself after firing."""
    config = settings()
    config.require("scheduler_group", "scheduler_role_arn")
    request: dict[str, Any] = {
        "Name": _auto_stop_name(channel),
        "GroupName": config.scheduler_group,
        "ScheduleExpression": f"at({at.astimezone(UTC):%Y-%m-%dT%H:%M:%S})",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "State": "ENABLED",
        "Description": f"Auto-stop of {channel} (channels.max_runtime_hours); correlation id {ctx.correlation_id}",
        "Target": {
            "Arn": STOP_CHANNEL_TARGET,
            "RoleArn": config.scheduler_role_arn,
            "Input": json.dumps({"ChannelName": channel}),
            "RetryPolicy": {"MaximumRetryAttempts": 5, "MaximumEventAgeInSeconds": 3600},
        },
    }
    scheduler = aws.client("scheduler")
    try:
        scheduler.create_schedule(**request)
    except ClientError as error:
        if channel_ops.error_code(error) != "ConflictException":
            raise
        scheduler.update_schedule(**request)  # a restart moves the pending auto-stop


def cancel_auto_stop(channel: str) -> None:
    if group := settings().scheduler_group:
        _ignore_missing(aws.client("scheduler").delete_schedule, Name=_auto_stop_name(channel), GroupName=group)


def _mark_skipped(program: store.ProgramRecord) -> store.ProgramRecord | None:
    """``program`` stored as SKIPPED, unless the as-run log marked it AIRED (it was removed so as not to replay)."""
    if program.status is store.ProgramStatus.AIRED:
        return None
    try:
        return store.put(
            program.model_copy(update={"status": store.ProgramStatus.SKIPPED}),
            condition=Attr("status").ne(store.ProgramStatus.AIRED.value),
        )
    except ClientError as error:
        if channel_ops.error_code(error) != "ConditionalCheckFailedException":
            raise
        return None


def skip_missed(record: store.ChannelRecord, ctx: ToolContext) -> list[store.ProgramRecord]:
    """Remove the programs of a stopped channel whose planned start has passed, before the channel starts.

    Channel Assembly plays a stopped channel's schedule from its head: a missed or in-progress program would start
    with the channel and be cut at the next program's ABSOLUTE start, and an aired program it has not deleted yet
    would replay (channel-assembly.md §5.3, verified live). Starts are the media library's, else Channel
    Assembly's; a RELATIVE program the library does not know is listed at 1970 and stays, as its air time is
    unknown. Programs that start later are never touched.

    Those that have not aired are marked SKIPPED first (whatever happens next, they will not air: every start
    removes them) and returned; the guide is rewritten once they are gone. Raises a retryable ToolError when the
    function runs out of time; calling again removes the rest.
    """
    hint = f"Wait a minute, then start {record.name} again."
    with lease(record.name, f"start.{ctx.correlation_id}", hint=hint):
        at = now_ms()
        known = {p.name: p for p in store.programs(record.name, start_ms=max(0, at - PROGRAM_LOOKBACK_MS))}
        live = channel_ops.live_programs(record.name, known_starts={name: p.start_ms for name, p in known.items()})
        missed = [p.name for p in live if p.anchored and p.start_ms < at]
        if not missed:
            return []
        skipped = [s for s in (_mark_skipped(known[name]) for name in missed if name in known) if s is not None]
        removed = channel_ops.delete_programs(
            record.name, missed, out_of_time=lambda: ctx.out_of_time(START_TIME_MARGIN_MS)
        )
        write_guide(record, at)
    logger.info(
        "missed programs removed",
        extra={"channel": record.name, "removed": removed, "skipped": [p.name for p in skipped]},
    )
    if len(removed) < len(missed):
        raise ToolError(
            "UPSTREAM",
            f"removed {len(removed)} of the {len(missed)} programs of {record.name} whose start has passed, then "
            "ran out of time; the channel was not started",
            retryable=True,
            hint=f"Call channels___start_channel again: it removes the rest, then starts {record.name}.",
        )
    return skipped


def _late_start_hint(skipped: list[SkippedProgram]) -> str | None:
    if not skipped:
        return None
    titles = "; ".join(f"{p.title} ({p.start})" for p in skipped)
    return (
        f"The channel started after the planned start of {len(skipped)} program(s), which will not air (SKIPPED "
        f"in the guide): {titles}. Every later program airs at its planned time, with filler slate until the next "
        "one; a new block (schedule___plan) can fill the gap."
    )


@tool("channels", access=Access.WRITE, approval=Approval.CONFIRM)
def start_channel(payload: ChannelInput, ctx: ToolContext) -> StartChannelOutput:
    """Start a channel so viewers can watch it. It is billed per hour while running (BASIC ~$0.10/h, STANDARD
    ~$0.35/h) and is stopped automatically after the deployment's maximum runtime (auto_stop_at); starting a running
    channel only moves the auto-stop. A stopped channel first drops the programs whose planned start has passed,
    which would otherwise air late: they are returned in skipped (SKIPPED in the guide) with a hint to relay to the
    operator, and every later program airs at its planned time. Returns the channel's alerts: SCHEDULING_ERROR
    alerts break playback."""
    record = require_channel(payload.channel)
    state = channel_ops.channel_state(record.name)
    if state is None:
        raise ToolError("NOT_FOUND", f"channel {record.name} does not exist in Channel Assembly")
    skipped = [
        SkippedProgram(**guide_entry(p).model_dump(include={"name", "title", "start", "end"}))
        for p in (skip_missed(record, ctx) if state == "STOPPED" else [])
    ]
    stop_at = None
    if (hours := settings().auto_stop_hours) > 0:
        when = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)
        schedule_auto_stop(record.name, at=when, ctx=ctx)  # before the start: billing never runs uncapped
        stop_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        channel_ops.start_channel(record.name)  # idempotent
    except ChannelStateTimeoutError as error:
        raise _in_transition(error, record.name) from error
    except ClientError as error:
        if is_missing(error):  # deleted meanwhile: the dispatcher reports NOT_FOUND
            cancel_auto_stop(record.name)
        raise
    logger.info("channel started", extra={"channel": record.name, "auto_stop_at": stop_at})
    alerts = after_change(lambda: channel_ops.channel_arn(record.name, record.arn))
    return StartChannelOutput(
        channel=record.name,
        state="RUNNING",
        auto_stop_at=stop_at,
        alerts=alerts,
        skipped=skipped or None,
        hint=_late_start_hint(skipped),
    )


@tool("channels", access=Access.WRITE, approval=Approval.CONFIRM)
def stop_channel(payload: ChannelInput, ctx: ToolContext) -> StateChange:
    """Stop a channel: playback ends for every viewer and billing stops. Published programs stay in the schedule
    and the guide. When the channel is started again, the programs whose start has passed by then are skipped and
    the rest air at their planned times."""
    record = require_channel(payload.channel)
    try:
        stopped = channel_ops.stop_channel(record.name)
    except ChannelStateTimeoutError as error:
        raise _in_transition(error, record.name) from error
    if not stopped:
        raise ToolError("NOT_FOUND", f"channel {record.name} does not exist in Channel Assembly")
    cancel_auto_stop(record.name)
    logger.info("channel stopped", extra={"channel": record.name})
    return StateChange(channel=record.name, state="STOPPED")


# --- create / delete -------------------------------------------------------------------------------------------------


class PlaybackTemplate(BaseModel):
    """``PLAYBACK_CONFIG_TEMPLATE`` (``infra.constructs.channel_assembly.playback_configuration_template``): what
    every agent channel's playback configuration shares."""

    content_base_url: str = Field(description="CDN-neutral base of content and ad segments.")
    log_percent: int = Field(ge=0, le=100)
    ads_url: str | None = Field(None, description="External VAST ADS; None = the ladder's preconditioned pod.")
    log_destination_arn: str = Field(description="Shared vended-logs delivery destination.")
    log_types: dict[str, str] = Field(description="Vended-log type -> delivery-source name suffix.")

    def delivery_sources(self, playback_configuration: str) -> dict[str, str]:
        """Delivery-source name per log type: ``<name>-<suffix>`` (<= 60 chars: names are at most 47 + 10)."""
        return {log_type: f"{playback_configuration}-{suffix}" for log_type, suffix in self.log_types.items()}


def _template() -> PlaybackTemplate:
    config = settings()
    config.require("playback_config_template")
    return PlaybackTemplate.model_validate_json(config.playback_config_template)


def _ready_ladder(ladder_version: str) -> tuple[store.LadderRecord, Ladder]:
    record = store.get_ladder(ladder_version)
    ladder = definition(record) if record is not None else None
    if record is None or ladder is None or record.status is not store.LadderStatus.READY or not record.kit.filler:
        status = record.status if record else "unknown"
        raise ToolError(
            "VALIDATION",
            f"ladder {ladder_version} is {status}; a channel needs a READY ladder with its filler slate",
            hint="Use a READY version from encoding___list_ladders, or provision one first.",
        )
    return record, ladder


def _outputs(requested: list[str] | None, ladder: Ladder) -> list[str]:
    """The channel's outputs: package profiles with renditions on ``ladder`` (the deployment registers at most five,
    the per-channel output quota)."""
    config = settings()
    outputs = list(dict.fromkeys(requested or config.package_profiles))
    usable = {p.name for p in applicable(ladder)} & set(config.package_profiles)
    if unknown := [o for o in outputs if o not in usable]:
        raise ToolError(
            "VALIDATION",
            f"outputs {unknown} are not package profiles of this deployment with renditions on {ladder.version}",
            hint=f"Choose from {sorted(usable)}.",
        )
    return outputs


def _check_quota(name: str) -> None:
    if store.get_channel(name) is not None:
        raise ToolError("CONFLICT", f"channel {name} already exists", hint="Choose another name.")
    owned = store.list_channels(store.Owner.AGENT)
    if len(owned) >= (limit := settings().max_agent_channels):
        raise ToolError(
            "QUOTA",
            f"the deployment allows {limit} agent channels and has {len(owned)}",
            hint="Delete an agent channel first (channels___delete_channel).",
        )


def _best_effort(operation: Callable[..., Any], **request: Any) -> Callable[[], None]:
    """An undo step for a failed create: logs instead of raising, so the original error surfaces."""

    def undo() -> None:
        try:
            operation(**request)
        except ClientError:
            logger.exception("rollback step failed", extra={"operation": getattr(operation, "__name__", "")})

    return undo


def _deliver_logs(undo: ExitStack, playback_configuration_arn: str, name: str, tags: dict[str, str]) -> None:
    logs, template = aws.client("logs"), _template()
    for log_type, source in template.delivery_sources(name).items():
        logs.put_delivery_source(name=source, resourceArn=playback_configuration_arn, logType=log_type, tags=tags)
        undo.callback(_best_effort(logs.delete_delivery_source, name=source))
        delivery = logs.create_delivery(
            deliverySourceName=source, deliveryDestinationArn=template.log_destination_arn, tags=tags
        )
        undo.callback(_best_effort(logs.delete_delivery, id=delivery["delivery"]["id"]))


def _playback_configuration(
    undo: ExitStack, *, name: str, channel_url: str, ladder_version: str, tags: dict[str, str]
) -> dict[str, Any]:
    """PutPlaybackConfiguration + its logging configuration from ``core.specs``; returns the API response."""
    template = _template()
    api = specs.playback_configuration(
        name=name,
        video_content_source_url=channel_url,
        ads_url=template.ads_url
        or specs.ads_url(content_base_url=template.content_base_url, ladder_version=ladder_version, channel=name),
        content_base_url=template.content_base_url,
        log_percent=template.log_percent,
        tags=tags,
    )
    put, configure_logs = specs.playback_configuration_requests(api)
    mediatailor = aws.client("mediatailor")
    response = mediatailor.put_playback_configuration(**put)
    undo.callback(_best_effort(mediatailor.delete_playback_configuration, Name=name))
    mediatailor.configure_logs_for_playback_configuration(**configure_logs)
    return dict(response)


def _channel_url(created: dict[str, Any]) -> str:
    """``https://<host>/v1/channel/<name>/``: every output's PlaybackUrl minus its manifest file name."""
    return str(created["Outputs"][0]["PlaybackUrl"]).rpartition("/")[0] + "/"


@tool("channels", access=Access.WRITE, approval=Approval.CONFIRM)
def create_channel(payload: CreateChannelInput, ctx: ToolContext) -> ChannelDetail:
    """Create an agent-owned linear channel <prefix>-agent-<name> with its ad-insertion playback configuration
    (SSAI and SGAI), logs and channel policy. The ladder version must be READY (encoding___list_ladders), and the
    channel airs only assets encoded with it. The channel is created STOPPED and costs nothing until
    channels___start_channel; fill it with schedule___plan and schedule___publish. Fails with QUOTA at the
    deployment's agent-channel limit and CONFLICT when the name is taken."""
    config = settings()
    config.require("source_location", "library_table")
    name = agent_owned_name(config.prefix, payload.name.removeprefix(config.agent_prefix))
    ladder_version = payload.ladder_version or config.default_ladder_version
    ladder_record, ladder = _ready_ladder(ladder_version)
    outputs = _outputs(payload.outputs, ladder)
    _check_quota(name)
    tags = _tags(ctx, ladder_version)
    mediatailor = aws.client("mediatailor")
    with ExitStack() as undo:
        created = mediatailor.create_channel(
            ChannelName=name,
            PlaybackMode="LINEAR",
            Tier=payload.tier,
            Outputs=specs.channel_outputs([profile(o) for o in outputs]),
            FillerSlate={"SourceLocationName": config.source_location, "VodSourceName": ladder_record.kit.filler},
            Tags=tags,
        )
        undo.callback(_best_effort(mediatailor.delete_channel, ChannelName=name))
        mediatailor.configure_logs_for_channel(ChannelName=name, LogTypes=["AS_RUN"])
        pc = _playback_configuration(
            undo, name=name, channel_url=_channel_url(created), ladder_version=ladder_version, tags=tags
        )
        _deliver_logs(undo, pc["PlaybackConfigurationArn"], name, tags)
        policy = specs.channel_policy(
            channel_arn=created["Arn"],
            playback_configuration_arn=pc["PlaybackConfigurationArn"],
            account=created["Arn"].split(":")[4],
        )
        mediatailor.put_channel_policy(ChannelName=name, Policy=json.dumps(policy))
        undo.callback(_best_effort(mediatailor.delete_channel_policy, ChannelName=name))
        record = store.put(
            store.ChannelRecord(
                name=name,
                ladder_version=ladder_version,
                outputs=outputs,
                owner=store.Owner.AGENT,
                tier=payload.tier,
                arn=created["Arn"],
                playback_configuration_name=name,
                hls_prefix=pc["HlsConfiguration"]["ManifestEndpointPrefix"],
                brief=payload.brief,
                correlation_id=ctx.correlation_id,
            ),
            condition=Attr("pk").not_exists(),
        )
        undo.pop_all()
    logger.info("agent channel created", extra={"channel": name, "ladder_version": ladder_version})
    return _detail(record, {"ChannelState": created["ChannelState"], "Arn": created["Arn"]})


def _delete_vended_logs(playback_configuration: str) -> None:
    logs = aws.client("logs")
    sources = set(_template().delivery_sources(playback_configuration).values())
    for page in logs.get_paginator("describe_deliveries").paginate():
        for delivery in page.get("deliveries", []):
            if delivery.get("deliverySourceName") in sources:
                _ignore_missing(logs.delete_delivery, id=delivery["id"])
    for source in sorted(sources):
        _ignore_missing(logs.delete_delivery_source, name=source)


def _channel_items(channel: str) -> Iterator[store.ItemKey]:
    for item in store.query_all(KeyConditionExpression=Key("pk").eq(store.channel_key(channel)["pk"])):
        yield {"pk": item["pk"], "sk": item["sk"]}


@tool("channels", access=Access.DESTRUCTIVE, approval=Approval.CONFIRM)
def delete_channel(payload: ChannelInput, ctx: ToolContext) -> DeleteChannelOutput:
    """Delete an agent-owned channel (<prefix>-agent-*): stop it, then remove its programs, guide, playback
    configuration and log deliveries. Cannot be undone. Channels defined by the deployment (owner iac) cannot be
    deleted; stop them instead."""
    config = settings()
    config.require("library_table", "media_bucket")
    record = require_channel(payload.channel)
    if record.owner is not store.Owner.AGENT or not is_agent_owned(record.name, config.prefix):
        raise ToolError(
            "FORBIDDEN",
            f"{record.name} is owned by the deployment ({record.owner}); only {config.agent_prefix}* channels "
            "can be deleted",
            hint="Stop it instead, or remove it from cdk.json and redeploy.",
        )
    mediatailor = aws.client("mediatailor")
    try:
        drained = channel_ops.drain_channel(
            record.name, table_name=config.library_table, out_of_time=lambda: ctx.out_of_time(DRAIN_TIME_MARGIN_MS)
        )
    except ChannelStateTimeoutError as error:
        raise _in_transition(error, record.name) from error
    if not drained.complete:
        raise ToolError(
            "UPSTREAM",
            f"deleted {drained.deleted} programs of {record.name} before running out of time; "
            f"{drained.remaining} remain",
            retryable=True,
            hint="Call channels___delete_channel again: it resumes with the programs that remain.",
        )
    cancel_auto_stop(record.name)
    _ignore_missing(mediatailor.delete_channel_policy, ChannelName=record.name)
    playback_configuration = record.playback_configuration_name or record.name
    _delete_vended_logs(playback_configuration)
    _ignore_missing(mediatailor.delete_playback_configuration, Name=playback_configuration)
    _ignore_missing(mediatailor.delete_channel, ChannelName=record.name)
    with store.table().batch_writer() as batch:
        for key in _channel_items(record.name):
            batch.delete_item(Key=key)
    epg.delete(record.name, bucket=config.media_bucket)
    logger.info("agent channel deleted", extra={"channel": record.name, "programs_deleted": drained.deleted})
    return DeleteChannelOutput(channel=record.name, programs_deleted=drained.deleted)


handler = dispatch.handler("channels")
