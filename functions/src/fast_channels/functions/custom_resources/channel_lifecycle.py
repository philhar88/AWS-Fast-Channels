"""Custom resources around an IaC linear channel (``LinearChannel`` in ``fast_channels.infra``).

One Lambda serves three resource types, placed at different points of the channel's dependency chain:

``Custom::ChannelGuard`` (the channel depends on it)
    Channel Assembly rejects any change to a running channel ("Running channels cannot be updated",
    channel-assembly.md §4.1). When a stack update changes the channel, the guard fails first with what to do, or
    stops the channel when ``RestartOnUpdate`` is ``true``. It never acts while its stack rolls back: CloudFormation
    then re-applies the previous properties, which the channel still has.
``Custom::ChannelEndpoint`` (depends on the channel)
    Returns the channel's base playback URL, which CloudFormation does not expose (only ``Arn``,
    channel-assembly.md §1.2). The playback configuration uses it as its content source.
``Custom::ChannelLifecycle`` (depends on the playback configuration and the channel policy)
    Writes the ``CHANNEL#<name>`` / ``META`` item of the media library and restarts a channel the guard stopped.
    Deleting it stops the channel and deletes all of its programs before CloudFormation deletes the channel:
    programs orphaned by DeleteChannel lock their VOD sources for 6-8 minutes (channel-assembly.md §9.7).

Channel Assembly creates channels stopped and never starts them on its own; this module only restarts a channel
that it stopped itself.
"""

import json
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.data_classes import CloudFormationCustomResourceEvent, event_source
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fast_channels.functions import aws

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

GUARD: Final = "Custom::ChannelGuard"
ENDPOINT: Final = "Custom::ChannelEndpoint"
LIFECYCLE: Final = "Custom::ChannelLifecycle"

SCHEDULE_MINUTES: Final = 14 * 24 * 60
"""GetChannelSchedule look-ahead when collecting programs to delete: the API maximum ("The duration-minutes can be
up to 20160 minutes (14 days)", observed live). The media library's PROGRAM items cover anything beyond."""
DELETE_PROGRAM_INTERVAL_S: Final = 0.34
"""DeleteProgram is limited to 3 TPS per account (channel-assembly.md §5.6)."""
STATE_POLL_S: Final = 2.0
STATE_TIMEOUT_S: Final = 120.0
ROLLBACK_STATUSES: Final = frozenset({"UPDATE_ROLLBACK_IN_PROGRESS", "ROLLBACK_IN_PROGRESS"})
CONFLICT_RETRIES: Final = 5
"""ConflictException ("The channel schedule has changed since your last request") is not retried by botocore."""

logger = Logger()


class ChannelRunningError(RuntimeError):
    """An update would change a running channel, which Channel Assembly refuses."""


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
            return lifecycle(event)
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
    stopped = ""
    changes_channel = event.request_type == "Update" and props["Spec"] != event.old_resource_properties.get("Spec")
    if changes_channel and channel_state(channel) == "RUNNING" and not rolling_back(event.stack_id):
        if props.get("RestartOnUpdate") != "true":
            raise ChannelRunningError(
                f"Channel {channel} is RUNNING, and Channel Assembly cannot change a running channel. Stop it "
                f"(channels___stop_channel, or `aws mediatailor stop-channel --channel-name {channel}`) and deploy "
                "again, or pass restart_on_update=True to its LinearChannel so deployments stop and restart it."
            )
        stop_channel(channel)
        stopped = event.request_id
        logger.info("channel stopped for update")
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


def lifecycle(event: CloudFormationCustomResourceEvent) -> dict[str, Any]:
    """Write the channel record (restarting the channel after a guarded update), or drain and forget the channel."""
    props, old = event.resource_properties, event.old_resource_properties
    channel, table_name = props["ChannelName"], props["TableName"]
    if event.request_type == "Delete":
        drain_channel(channel, table_name=table_name)
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


# --- media library -------------------------------------------------------------------------------------------------


def channel_key(channel: str) -> dict[str, str]:
    """Key of a channel's ``META`` item (CONTRACTS §D)."""
    return {"pk": f"CHANNEL#{channel}", "sk": "META"}


def write_record(table_name: str, record: Mapping[str, Any], *, brief: str | None, correlation_id: str) -> None:
    """Upsert the IaC-owned attributes of a channel's ``META`` item; other attributes are left alone.

    ``brief`` is the programming brief as JSON: ``None`` keeps the stored brief (the agent may have updated it at
    runtime), ``""`` removes it. The attribute names are those of the tools' ``ChannelRecord``.
    """
    name = record["name"]
    attributes: dict[str, Any] = {
        **record,
        "owner": "iac",
        "gsi1pk": "CHANNELS",
        "gsi1sk": f"iac#{name}",
        "correlation_id": correlation_id,
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    removed: list[str] = []
    if brief:
        attributes["brief"] = json.loads(brief, parse_float=Decimal)
    elif brief == "":
        removed.append("brief")
    aws.table(table_name).update_item(Key=channel_key(name), **update_expression(attributes, removed))


def update_expression(attributes: Mapping[str, Any], removed: Iterable[str] = ()) -> dict[str, Any]:
    """UpdateItem arguments setting ``attributes`` and removing ``removed`` (every name aliased: ``name``,
    ``owner`` and ``status`` are reserved words)."""
    names = {f"#a{i}": name for i, name in enumerate(attributes)}
    expression = "SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attributes)))
    values = {f":a{i}": value for i, value in enumerate(attributes.values())}
    if removed := list(removed):
        names |= {f"#r{i}": name for i, name in enumerate(removed)}
        expression += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(removed)))
    return {"UpdateExpression": expression, "ExpressionAttributeNames": names, "ExpressionAttributeValues": values}


# --- channel operations (shared with the reaper) -------------------------------------------------------------------


def error_code(error: ClientError) -> str:
    """AWS error code; MediaTailor's NotFoundException is not modelled in boto3 (e2e.md §8 item 4)."""
    return str(error.response.get("Error", {}).get("Code", ""))


def channel_state(channel: str) -> str | None:
    """``RUNNING`` or ``STOPPED``; ``None`` when the channel does not exist."""
    try:
        return str(aws.client("mediatailor").describe_channel(ChannelName=channel)["ChannelState"])
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            return None
        raise


def stop_channel(channel: str) -> bool:
    """Stop ``channel`` (idempotent) and wait until it is stopped; ``False`` when it does not exist."""
    try:
        aws.client("mediatailor").stop_channel(ChannelName=channel)
    except ClientError as error:
        if error_code(error) == "NotFoundException":
            return False
        raise
    wait_for_state(channel, "STOPPED")
    return True


def start_channel(channel: str) -> None:
    """Start ``channel`` unless it runs already, and wait until it runs."""
    if channel_state(channel) == "RUNNING":
        return
    aws.client("mediatailor").start_channel(ChannelName=channel)
    wait_for_state(channel, "RUNNING")


def rolling_back(stack_id: str) -> bool:
    """Whether the stack is rolling back (an update, or a failed creation)."""
    [stack] = aws.client("cloudformation").describe_stacks(StackName=stack_id)["Stacks"]
    return stack["StackStatus"] in ROLLBACK_STATUSES


def wait_for_state(channel: str, state: str) -> None:
    """Poll DescribeChannel until ``channel`` reaches ``state``."""
    deadline = time.monotonic() + STATE_TIMEOUT_S
    while (current := channel_state(channel)) != state:
        if time.monotonic() > deadline:
            raise TimeoutError(f"channel {channel} is still {current} after {STATE_TIMEOUT_S:.0f} s")
        time.sleep(STATE_POLL_S)


def drain_channel(channel: str, *, table_name: str | None = None) -> int:
    """Stop ``channel`` and delete all of its programs; returns how many were deleted (0 if it does not exist).

    Programs come from the channel schedule and, with ``table_name``, from the media library's PROGRAM items: the
    schedule lists only what falls within its look-ahead, and stopped channels report RELATIVE programs at 1970.
    DeleteProgram is idempotent (a missing program is a success).
    """
    if not stop_channel(channel):
        return 0
    programs = scheduled_programs(channel) | (library_programs(table_name, channel) if table_name else set())
    for program in sorted(programs):
        delete_program(channel, program)
        time.sleep(DELETE_PROGRAM_INTERVAL_S)
    logger.info("channel drained", extra={"programs_deleted": len(programs)})
    return len(programs)


def scheduled_programs(channel: str) -> set[str]:
    """Names of the programs in the channel schedule."""
    pages = aws.client("mediatailor").get_paginator("get_channel_schedule")
    return {
        item["ProgramName"]
        for page in pages.paginate(ChannelName=channel, DurationMinutes=str(SCHEDULE_MINUTES))
        for item in page["Items"]
        if item.get("ProgramName")
    }


def library_programs(table_name: str, channel: str) -> set[str]:
    """Names of the channel's PROGRAM items in the media library (the EPG source of truth)."""
    request: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(f"CHANNEL#{channel}") & Key("sk").begins_with("PROGRAM#"),
        "ProjectionExpression": "#n",
        "ExpressionAttributeNames": {"#n": "name"},
    }
    names: set[str] = set()
    while True:
        page = aws.table(table_name).query(**request)
        names |= {str(item["name"]) for item in page.get("Items", []) if item.get("name")}
        if not (start := page.get("LastEvaluatedKey")):
            return names
        request["ExclusiveStartKey"] = start


def delete_program(channel: str, program: str) -> None:
    """Delete one program, retrying schedule conflicts with exponential backoff."""
    for attempt in range(CONFLICT_RETRIES):
        try:
            aws.client("mediatailor").delete_program(ChannelName=channel, ProgramName=program)
            return
        except ClientError as error:
            if error_code(error) != "ConflictException" or attempt == CONFLICT_RETRIES - 1:
                raise
            time.sleep(2**attempt)
