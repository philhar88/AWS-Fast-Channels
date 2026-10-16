"""Custom resources around an IaC linear channel (``LinearChannel`` in ``fast_channels.infra``).

One Lambda serves three resource types, placed at different points of the channel's dependency chain:

``Custom::ChannelGuard`` (the channel depends on it)
    Channel Assembly rejects any change to a running channel ("Running channels cannot be updated",
    channel-assembly.md §4.1). When a stack update changes the channel, the guard fails first with what to do, or
    stops the channel when ``RestartOnUpdate`` is ``true``. A rollback is let through while the channel still has
    the properties CloudFormation re-applies; when the update being rolled back restarted the channel with its new
    properties, the guard stops it again, and the lifecycle resource restarts it once they are restored.
``Custom::ChannelEndpoint`` (depends on the channel)
    Returns the channel's base playback URL, which CloudFormation does not expose (only ``Arn``,
    channel-assembly.md §1.2). The playback configuration uses it as its content source.
``Custom::ChannelLifecycle`` (depends on the playback configuration and the channel policy)
    Writes the ``CHANNEL#<name>`` / ``META`` item of the media library and restarts a channel the guard stopped.
    Deleting it stops the channel and deletes its programs before CloudFormation deletes the channel: programs
    orphaned by DeleteChannel lock their VOD sources for 6-8 minutes (channel-assembly.md §9.7). A drain that runs
    out of time fails the deletion; deleting again resumes it.

Channel Assembly creates channels stopped and never starts them on its own; this module only restarts a channel
that it stopped itself. The channel operations are :mod:`fast_channels.functions.channel_ops`'.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.data_classes import CloudFormationCustomResourceEvent, event_source

from fast_channels.core.tags import Owner
from fast_channels.functions import aws
from fast_channels.functions.channel_ops import channel_state, drain_channel, error_code, start_channel, stop_channel
from fast_channels.functions.media_library import channel_index, channel_key, now_iso, update_request

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

__all__ = [
    "ENDPOINT",
    "GUARD",
    "LIFECYCLE",
    "ChannelDrainIncompleteError",
    "ChannelRunningError",
    "endpoint",
    "error_code",
    "guard",
    "handler",
    "lifecycle",
    "rolling_back",
    "write_record",
]

GUARD: Final = "Custom::ChannelGuard"
ENDPOINT: Final = "Custom::ChannelEndpoint"
LIFECYCLE: Final = "Custom::ChannelLifecycle"
ROLLBACK_STATUSES: Final = frozenset({"UPDATE_ROLLBACK_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"})
TIME_MARGIN_MS: Final = 30_000
"""Stop deleting programs this long before the invocation times out, so the failure is reported."""

logger = Logger()


class ChannelRunningError(RuntimeError):
    """An update would change a running channel, which Channel Assembly refuses."""


class ChannelDrainIncompleteError(RuntimeError):
    """The channel's programs could not all be deleted within one invocation; deleting again resumes."""


@logger.inject_lambda_context(correlation_id_path="RequestId")
@event_source(data_class=CloudFormationCustomResourceEvent)
def handler(event: CloudFormationCustomResourceEvent, context: LambdaContext) -> dict[str, Any]:
    """Provider-framework entry point for the three channel resource types."""
    logger.append_keys(channel=event.resource_properties.get("ChannelName"), resource_type=event.resource_type)
    logger.info("custom resource request", extra={"request_type": event.request_type})
    match event.resource_type:
        case "Custom::ChannelGuard":
            return guard(event)
        case "Custom::ChannelEndpoint":
            return endpoint(event)
        case "Custom::ChannelLifecycle":
            return lifecycle(event, context)
        case other:
            raise ValueError(f"unsupported resource type {other}")


# --- resource types ------------------------------------------------------------------------------------------------


def guard(event: CloudFormationCustomResourceEvent) -> dict[str, Any]:
    """Stop (``RestartOnUpdate``) or refuse an update that changes a running channel.

    ``Data.StoppedForUpdate`` is this request's id when the guard stopped the channel, else empty; the lifecycle
    resource restarts the channel when that value changes.
    """
    props = event.resource_properties
    channel = props["ChannelName"]
    changes_channel = event.request_type == "Update" and props["Spec"] != event.old_resource_properties.get("Spec")
    if not changes_channel or channel_state(channel) != "RUNNING":
        return _guarded(channel, stopped="")
    if rolling_back(event.stack_id):
        # The update being rolled back (its properties are the "old" ones now) restarted the channel only if it
        # allowed restarts; otherwise the channel still has the properties CloudFormation re-applies.
        if event.old_resource_properties.get("RestartOnUpdate") != "true":
            return _guarded(channel, stopped="")
    elif props.get("RestartOnUpdate") != "true":
        raise ChannelRunningError(
            f"Channel {channel} is RUNNING, and Channel Assembly cannot change a running channel. Stop it "
            f"(channels___stop_channel, or `aws mediatailor stop-channel --channel-name {channel}`) and deploy "
            "again, or, for a LinearChannel built in code, set restart_on_update=True so that deployments stop "
            "and restart it."
        )
    stop_channel(channel)
    logger.info("channel stopped for update")
    return _guarded(channel, stopped=event.request_id)


def _guarded(channel: str, *, stopped: str) -> dict[str, Any]:
    return {"PhysicalResourceId": channel, "Data": {"StoppedForUpdate": stopped}}


def endpoint(event: CloudFormationCustomResourceEvent) -> dict[str, Any]:
    """``Data.ChannelUrl``: ``https://<host>/v1/channel/<name>/``, the playback configuration's content source."""
    channel = event.resource_properties["ChannelName"]
    if event.request_type == "Delete":
        return {"PhysicalResourceId": event.physical_resource_id}
    outputs = aws.client("mediatailor").describe_channel(ChannelName=channel)["Outputs"]
    # PlaybackUrl = <base>/<ManifestName>.m3u8|.mpd; the host is shared by every channel of the account and region.
    bases = {output["PlaybackUrl"].rpartition("/")[0] + "/" for output in outputs}
    if len(bases) != 1:
        raise RuntimeError(f"channel {channel} outputs do not share one base URL: {sorted(bases)}")
    return {"PhysicalResourceId": channel, "Data": {"ChannelUrl": bases.pop()}}


def lifecycle(event: CloudFormationCustomResourceEvent, context: LambdaContext) -> dict[str, Any]:
    """Write the channel record (restarting the channel after a guarded update), or drain and forget the channel."""
    props, old = event.resource_properties, event.old_resource_properties
    channel, table_name = props["ChannelName"], props["TableName"]
    if event.request_type == "Delete":
        drained = drain_channel(
            channel,
            table_name=table_name,
            out_of_time=lambda: context.get_remaining_time_in_millis() < TIME_MARGIN_MS,
        )
        if not drained.complete:
            raise ChannelDrainIncompleteError(
                f"deleted {drained.deleted} programs of {channel}, {drained.remaining} remain: delete the stack "
                "again to resume"
            )
        aws.table(table_name).delete_item(Key=channel_key(channel))
        logger.info("channel record deleted")
        return {"PhysicalResourceId": event.physical_resource_id}

    brief_changed = event.request_type == "Create" or props.get("Brief", "") != old.get("Brief", "")
    write_record(
        table_name,
        props["Record"],
        brief=props.get("Brief", "") if brief_changed else None,
        correlation_id=event.request_id,
    )
    if (token := props.get("RestartToken")) and token != old.get("RestartToken"):
        start_channel(channel)
        logger.info("channel restarted after update")
    return {"PhysicalResourceId": channel}


def rolling_back(stack_id: str) -> bool:
    """Whether the stack is rolling back (an update, or a failed creation)."""
    [stack] = aws.client("cloudformation").describe_stacks(StackName=stack_id)["Stacks"]
    return stack["StackStatus"] in ROLLBACK_STATUSES


# --- media library -------------------------------------------------------------------------------------------------


def write_record(table_name: str, record: Mapping[str, Any], *, brief: str | None, correlation_id: str) -> None:
    """Upsert the IaC-owned attributes of a channel's ``META`` item; other attributes are left alone.

    ``brief`` is the programming brief as JSON: ``None`` keeps the stored brief (the agent may have updated it at
    runtime), ``""`` removes it. The attribute names are those of the tools' ``ChannelRecord``.
    """
    name = record["name"]
    attributes: dict[str, Any] = {
        **record,
        "owner": Owner.IAC.value,
        **channel_index(Owner.IAC, name),
        "correlation_id": correlation_id,
        "updated_at": now_iso(),
    }
    if brief:
        attributes["brief"] = json.loads(brief, parse_float=Decimal)
    removed = ["brief"] if brief == "" else []
    aws.table(table_name).update_item(**update_request(channel_key(name), attributes, remove=removed))
