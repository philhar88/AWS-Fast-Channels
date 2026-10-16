"""``channels`` tools: list, inspect, start and stop linear channels; create and delete agent-owned ones.

Channels defined in ``cdk.json`` are owned by CloudFormation (``owner = iac``); the agent may start and stop them but
creates and deletes only its own ``<prefix>-agent-*`` channels (the Gateway's Cedar policy enforces the same rule).
An agent channel is assembled from the same ``core.specs`` builders the CDK constructs use: a LINEAR channel with
the ladder kit's filler slate, AS_RUN logs, a per-channel PLAYER_SELECT playback configuration with its three
vended-log deliveries, and a SigV4-only channel policy for exactly that configuration (e2e.md §2.7-2.11).

A started channel bills by the hour, so ``start_channel`` also creates a one-time EventBridge Scheduler schedule that
stops it after ``channels.max_runtime_hours`` (verified: the universal target ``mediatailor:stopChannel`` stops the
channel and the schedule deletes itself).
"""

import json
import re
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
from fast_channels.core.timeline import ProgrammingBrief  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.functions import aws, epg
from fast_channels.functions.custom_resources import channel_lifecycle
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _library_store as store
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools.encoding import definition
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, tool
from fast_channels.functions.tools.schedule import Alert, ChannelInput, alerts, channel_arn, require_channel

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

logger = Logger()

STOP_CHANNEL_TARGET: Final = "arn:aws:scheduler:::aws-sdk:mediatailor:stopChannel"
"""EventBridge Scheduler universal target; the schedule's Input is the StopChannel request."""
_MISSING: Final = frozenset({"NotFoundException", "ResourceNotFoundException"})

type ChannelState = Literal["RUNNING", "STOPPED", "MISSING"]


def _code(error: ClientError) -> str:
    return channel_lifecycle.error_code(error)


def _ignore_missing(operation: Callable[..., Any], **request: Any) -> None:
    try:
        operation(**request)
    except ClientError as error:
        if _code(error) not in _MISSING:
            raise


def _tags(ctx: ToolContext, ladder_version: str) -> dict[str, str]:
    """CONTRACTS §E tags of an agent-created resource (the reaper deletes by ``fast:app``)."""
    return {
        "fast:app": settings().app,
        "fast:owner": store.Owner.AGENT.value,
        "fast:ladder": ladder_version,
        "fast:correlation-id": ctx.correlation_id,
    }


# --- models ----------------------------------------------------------------------------------------------------------


class ChannelSummary(BaseModel):
    name: str
    state: ChannelState = Field(description="MISSING: in the media library but not in Channel Assembly.")
    owner: store.Owner = Field(description="iac: defined by the deployment; agent: created with channels tools.")
    ladder_version: str
    outputs: list[str] = Field(description="Output (rendition profile) names, e.g. hls, hls-avc, dash-avc.")
    tier: str | None = Field(description="BASIC (~$0.10/h running) or STANDARD (~$0.35/h).")


class ListChannelsInput(BaseModel):
    owner: store.Owner | None = Field(None, description="Only channels of this owner.")


class ListChannelsOutput(BaseModel):
    channels: list[ChannelSummary]


class ChannelDetail(ChannelSummary):
    arn: str | None
    playback_configuration: str | None
    playback: dict[str, dict[str, str]] = Field(
        description="Viewer URLs per output through the CDN: ssai and sgai (HLS) or dash."
    )
    auto_stop_at: str | None = Field(description="UTC time the scheduled auto-stop fires; null when none.")


class StateChange(BaseModel):
    channel: str
    state: Literal["RUNNING", "STOPPED"]
    auto_stop_at: str | None = Field(None, description="UTC time the channel is stopped automatically.")
    alerts: list[Alert] = Field([], description="Channel alerts (harmless codes filtered out).")


class CreateChannelInput(BaseModel):
    name: str = Field(
        pattern=r"^[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?$",
        description="Short name; the channel is named <prefix>-agent-<name>.",
    )
    ladder_version: str | None = Field(None, description="READY ladder version; default: the deployment's default.")
    outputs: list[str] | None = Field(
        None, description="Outputs (rendition profiles), at most 5; default: every package profile of the deployment."
    )
    tier: Literal["BASIC", "STANDARD"] = Field("BASIC", description="BASIC ~$0.10/h, STANDARD ~$0.35/h, while running.")
    brief: ProgrammingBrief | None = Field(None, description="Programming brief for the schedule planner.")


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
    """List the deployment's channels with their live state (RUNNING bills by the hour), owner, ladder version and
    outputs."""
    states = _live_states()
    return ListChannelsOutput(
        channels=[ChannelSummary(**_summary(r, states.get(r.name))) for r in store.list_channels(payload.owner)]
    )


def _cdn_base() -> str:
    domain = settings().cdn_domain
    return domain if domain.startswith("https://") else f"https://{domain}"


def _prefixes(record: store.ChannelRecord) -> tuple[str, str] | None:
    if record.hls_prefix and record.dash_prefix:
        return record.hls_prefix, record.dash_prefix
    if not record.playback_configuration_name:
        return None
    try:
        config = aws.client("mediatailor").get_playback_configuration(Name=record.playback_configuration_name)
    except ClientError as error:
        if _code(error) in _MISSING:
            return None
        raise
    return config["HlsConfiguration"]["ManifestEndpointPrefix"], config["DashConfiguration"]["ManifestEndpointPrefix"]


def playback(record: store.ChannelRecord) -> dict[str, dict[str, str]]:
    """Viewer URLs per output (``core.specs.playback_urls``); empty without a playback configuration."""
    prefixes = _prefixes(record)
    if prefixes is None or not settings().cdn_domain:
        return {}
    hls, dash = prefixes
    return specs.playback_urls(
        cdn_base=_cdn_base(), playback_prefix_hls=hls, playback_prefix_dash=dash, outputs=record.outputs
    )


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
        if _code(error) in _MISSING:
            return None
        raise
    match = re.fullmatch(r"at\((.+)\)", schedule.get("ScheduleExpression", ""))
    return f"{match.group(1)}Z" if match else None


def _describe(name: str) -> dict[str, Any] | None:
    try:
        return dict(aws.client("mediatailor").describe_channel(ChannelName=name))
    except ClientError as error:
        if _code(error) in _MISSING:
            return None
        raise


def _detail(record: store.ChannelRecord, live: dict[str, Any] | None) -> ChannelDetail:
    return ChannelDetail(
        **_summary(record, live["ChannelState"] if live else None),
        arn=live["Arn"] if live else record.arn,
        playback_configuration=record.playback_configuration_name,
        playback=playback(record),
        auto_stop_at=auto_stop_at(record.name),
    )


@tool("channels", access=Access.READ)
def get_channel(payload: ChannelInput, ctx: ToolContext) -> ChannelDetail:
    """Get one channel: live state, owner, tier, ladder version, outputs, viewer playback URLs per output
    (SSAI and SGAI for HLS, DASH) and the pending auto-stop time."""
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
        if _code(error) != "ConflictException":
            raise
        scheduler.update_schedule(**request)  # a restart moves the pending auto-stop


def cancel_auto_stop(channel: str) -> None:
    if group := settings().scheduler_group:
        _ignore_missing(aws.client("scheduler").delete_schedule, Name=_auto_stop_name(channel), GroupName=group)


@tool("channels", access=Access.WRITE, approval=Approval.CONFIRM)
def start_channel(payload: ChannelInput, ctx: ToolContext) -> StateChange:
    """Start a channel so viewers can watch it; it bills by the hour while running and is stopped automatically
    after the deployment's maximum runtime (auto_stop_at). Starting a running channel only moves the auto-stop."""
    record = require_channel(payload.channel)
    channel_lifecycle.start_channel(record.name)  # idempotent; a missing channel raises NotFoundException
    stop_at = None
    if (hours := settings().auto_stop_hours) > 0:
        when = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=hours)
        schedule_auto_stop(record.name, at=when, ctx=ctx)
        stop_at = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    logger.info("channel started", extra={"channel": record.name, "auto_stop_at": stop_at})
    return StateChange(channel=record.name, state="RUNNING", auto_stop_at=stop_at, alerts=alerts(channel_arn(record)))


@tool("channels", access=Access.WRITE, approval=Approval.CONFIRM)
def stop_channel(payload: ChannelInput, ctx: ToolContext) -> StateChange:
    """Stop a channel (viewers get errors; billing stops). Programs stay scheduled; restarting replays the ones
    that have not aired."""
    record = require_channel(payload.channel)
    if not channel_lifecycle.stop_channel(record.name):
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
    """Create an agent-owned linear channel (<prefix>-agent-<name>) on a READY ladder, with its playback
    configuration, logs and channel policy. It is created STOPPED: start it with channels___start_channel."""
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
                dash_prefix=pc["DashConfiguration"]["ManifestEndpointPrefix"],
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
    for item in store.query_all(KeyConditionExpression=Key("pk").eq(f"CHANNEL#{channel}")):
        yield {"pk": item["pk"], "sk": item["sk"]}


@tool("channels", access=Access.DESTRUCTIVE, approval=Approval.CONFIRM)
def delete_channel(payload: ChannelInput, ctx: ToolContext) -> DeleteChannelOutput:
    """Delete an agent-owned channel (<prefix>-agent-*) with its programs, guide, playback configuration and logs
    deliveries. Channels defined by the deployment cannot be deleted."""
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
    programs = channel_lifecycle.drain_channel(record.name, table_name=config.library_table)
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
    logger.info("agent channel deleted", extra={"channel": record.name, "programs_deleted": programs})
    return DeleteChannelOutput(channel=record.name, programs_deleted=programs)


handler = dispatch.handler("channels")
