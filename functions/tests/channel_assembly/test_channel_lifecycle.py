import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from fast_channels.functions.custom_resources import channel_lifecycle
from fast_channels.functions.custom_resources.channel_lifecycle import (
    ENDPOINT,
    GUARD,
    LIFECYCLE,
    ChannelRunningError,
    handler,
    update_expression,
)

type Stub = Callable[[str], Stubber]
type Event = Callable[..., dict[str, Any]]

CHANNEL = "fast-demo"
BASE = "https://e4134234.7wzuvg.channel-assembly.mediatailor.us-east-1.amazonaws.com/v1/channel/fast-demo/"
SPEC = {"Tier": "BASIC", "Outputs": [{"ManifestName": "hls", "SourceGroup": "hls"}]}
RECORD = {
    "name": CHANNEL,
    "arn": "arn:aws:mediatailor:us-east-1:123456789012:channel/fast-demo",
    "tier": "BASIC",
    "ladder_version": "standard@86bf8088e0",
    "outputs": ["hls", "dash-avc"],
    "playback_configuration_name": CHANNEL,
    "hls_prefix": "https://abc.mediatailor.us-east-1.amazonaws.com/v1/master/h/fast-demo/",
    "dash_prefix": "https://abc.mediatailor.us-east-1.amazonaws.com/v1/dash/h/fast-demo/",
    "channel_url": BASE,
}
BRIEF = '{"break_policy": {"cadence_minutes": 12.5}, "ratings": ["TV-G"]}'


def describe(state: str, outputs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"ChannelState": state, "Outputs": outputs or [], "LogConfiguration": {"LogTypes": ["AS_RUN"]}}


def guard_event(cfn_event: Event, spec: dict[str, Any], *, restart: str = "false", old_spec: Any = SPEC) -> Any:
    properties = {"ChannelName": CHANNEL, "RestartOnUpdate": restart, "Spec": spec}
    return cfn_event(GUARD, "Update", properties, old={**properties, "Spec": old_spec}, physical_id=CHANNEL)


def lifecycle_props(**overrides: Any) -> dict[str, Any]:
    return {
        "ChannelName": CHANNEL,
        "TableName": "library",
        "RestartToken": "",
        "Brief": "",
        "Record": RECORD,
    } | overrides


# --- guard ---------------------------------------------------------------------------------------------------------


def test_guard_create_is_a_no_op(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("mediatailor")
    response = handler(cfn_event(GUARD, "Create", {"ChannelName": CHANNEL, "Spec": SPEC}), context)

    assert response == {"PhysicalResourceId": CHANNEL, "Data": {"StoppedForUpdate": ""}}


def stack_status(stub: Stub, status: str) -> None:
    stack = {"StackName": "Media", "CreationTime": datetime(2026, 10, 16, tzinfo=UTC), "StackStatus": status}
    stub("cloudformation").add_response("describe_stacks", {"Stacks": [stack]}, {"StackName": ANY})


def test_guard_fails_fast_on_a_running_channel(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("mediatailor").add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    stack_status(stub, "UPDATE_IN_PROGRESS")

    with pytest.raises(ChannelRunningError, match=r"fast-demo is RUNNING.*stop-channel.*restart_on_update=True"):
        handler(guard_event(cfn_event, {**SPEC, "Tier": "STANDARD"}), context)


def test_guard_stops_a_running_channel_when_allowed(stub: Stub, cfn_event: Event, context: Any) -> None:
    mediatailor = stub("mediatailor")
    mediatailor.add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    stack_status(stub, "UPDATE_IN_PROGRESS")
    mediatailor.add_response("stop_channel", {}, {"ChannelName": CHANNEL})
    mediatailor.add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    mediatailor.add_response("describe_channel", describe("STOPPED"), {"ChannelName": CHANNEL})

    response = handler(guard_event(cfn_event, {**SPEC, "Tier": "STANDARD"}, restart="true"), context)

    assert response["Data"] == {"StoppedForUpdate": "update-request"}


def test_guard_lets_a_rollback_restore_the_running_channel_spec(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("mediatailor").add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    stack_status(stub, "UPDATE_ROLLBACK_IN_PROGRESS")

    event = guard_event(cfn_event, SPEC, old_spec={**SPEC, "Tier": "STANDARD"})  # back to what the channel has

    assert handler(event, context)["Data"] == {"StoppedForUpdate": ""}


def test_guard_ignores_stopped_channels_and_unchanged_specs(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("mediatailor").add_response("describe_channel", describe("STOPPED"), {"ChannelName": CHANNEL})

    changed_but_stopped = handler(guard_event(cfn_event, {**SPEC, "Tier": "STANDARD"}), context)
    unchanged = handler(guard_event(cfn_event, SPEC, restart="true"), context)  # no DescribeChannel at all

    assert changed_but_stopped["Data"] == unchanged["Data"] == {"StoppedForUpdate": ""}


def test_guard_treats_a_new_channel_name_as_not_running(
    stub: Stub, cfn_event: Event, context: Any, client_error: Callable[..., dict[str, Any]]
) -> None:
    stub("mediatailor").add_client_error("describe_channel", **client_error("NotFoundException", status=404))

    assert handler(guard_event(cfn_event, {**SPEC, "Tier": "STANDARD"}), context)["Data"]["StoppedForUpdate"] == ""


# --- endpoint ------------------------------------------------------------------------------------------------------


def test_endpoint_returns_the_channel_base_url(stub: Stub, cfn_event: Event, context: Any) -> None:
    outputs = [
        {"ManifestName": m, "SourceGroup": m, "PlaybackUrl": f"{BASE}{m}{ext}"}
        for m, ext in (("hls", ".m3u8"), ("dash-avc", ".mpd"))
    ]
    stub("mediatailor").add_response("describe_channel", describe("STOPPED", outputs), {"ChannelName": CHANNEL})

    response = handler(cfn_event(ENDPOINT, "Create", {"ChannelName": CHANNEL, "Outputs": ["hls", "dash-avc"]}), context)

    assert response == {"PhysicalResourceId": CHANNEL, "Data": {"ChannelUrl": BASE}}


def test_endpoint_rejects_outputs_on_different_hosts(stub: Stub, cfn_event: Event, context: Any) -> None:
    outputs = [
        {"ManifestName": "a", "SourceGroup": "a", "PlaybackUrl": f"{BASE}a.m3u8"},
        {"ManifestName": "b", "SourceGroup": "b", "PlaybackUrl": "https://other/v1/channel/x/b.m3u8"},
    ]
    stub("mediatailor").add_response("describe_channel", describe("STOPPED", outputs), {"ChannelName": CHANNEL})

    with pytest.raises(RuntimeError, match="do not share one base URL"):
        handler(cfn_event(ENDPOINT, "Create", {"ChannelName": CHANNEL}), context)


def test_endpoint_delete_is_a_no_op(stub: Stub, cfn_event: Event, context: Any) -> None:
    stub("mediatailor")
    event = cfn_event(ENDPOINT, "Delete", {"ChannelName": CHANNEL}, physical_id=CHANNEL)

    assert handler(event, context) == {"PhysicalResourceId": CHANNEL}


# --- lifecycle -----------------------------------------------------------------------------------------------------


def record_update(*, brief: Any = None, remove_brief: bool = False) -> dict[str, Any]:
    """Expected UpdateItem parameters (the table resource's Stubber sees them before serialization)."""
    attributes = {
        **RECORD,
        "owner": "iac",
        "gsi1pk": "CHANNELS",
        "gsi1sk": f"iac#{CHANNEL}",
        "correlation_id": ANY,
        "updated_at": ANY,
    }
    if brief is not None:
        attributes["brief"] = brief
    expression = update_expression(attributes, ["brief"] if remove_brief else [])
    return {"TableName": "library", "Key": {"pk": f"CHANNEL#{CHANNEL}", "sk": "META"}, **expression}


def test_lifecycle_create_writes_the_channel_record_with_its_brief(
    stub: Stub, cfn_event: Event, context: Any, typed: Callable[..., dict[str, Any]]
) -> None:
    brief = {"break_policy": {"cadence_minutes": Decimal("12.5")}, "ratings": ["TV-G"]}
    stub("dynamodb").add_response("update_item", {}, record_update(brief=brief))
    stub("mediatailor")

    response = handler(cfn_event(LIFECYCLE, "Create", lifecycle_props(Brief=BRIEF)), context)

    assert response == {"PhysicalResourceId": CHANNEL}


def test_lifecycle_update_keeps_a_runtime_brief_and_restarts_a_stopped_channel(
    stub: Stub, cfn_event: Event, context: Any, typed: Callable[..., dict[str, Any]]
) -> None:
    stub("dynamodb").add_response("update_item", {}, record_update())  # brief unchanged: not written
    mediatailor = stub("mediatailor")
    mediatailor.add_response("describe_channel", describe("STOPPED"), {"ChannelName": CHANNEL})
    mediatailor.add_response("start_channel", {}, {"ChannelName": CHANNEL})
    mediatailor.add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    event = cfn_event(
        LIFECYCLE,
        "Update",
        lifecycle_props(Brief=BRIEF, RestartToken="guard-request"),
        old=lifecycle_props(Brief=BRIEF),
        physical_id=CHANNEL,
    )

    handler(event, context)


def test_lifecycle_update_removes_a_dropped_brief_without_restarting(
    stub: Stub, cfn_event: Event, context: Any, typed: Callable[..., dict[str, Any]]
) -> None:
    stub("dynamodb").add_response("update_item", {}, record_update(remove_brief=True))
    stub("mediatailor")
    props = lifecycle_props(RestartToken="earlier-guard-request")  # token unchanged: no restart
    event = cfn_event(LIFECYCLE, "Update", props, old={**props, "Brief": BRIEF}, physical_id=CHANNEL)

    handler(event, context)


def test_lifecycle_delete_stops_the_channel_and_deletes_every_program(
    stub: Stub,
    cfn_event: Event,
    context: Any,
    typed: Callable[..., dict[str, Any]],
    client_error: Callable[..., dict[str, Any]],
) -> None:
    mediatailor, dynamodb = stub("mediatailor"), stub("dynamodb")
    mediatailor.add_response("stop_channel", {}, {"ChannelName": CHANNEL})
    mediatailor.add_response("describe_channel", describe("STOPPED"), {"ChannelName": CHANNEL})
    schedule = {"ChannelName": CHANNEL, "DurationMinutes": "20160"}
    mediatailor.add_response(
        "get_channel_schedule",
        {
            "Items": [
                {"ProgramName": "p1", **_entry()},
                {**_entry(), "ProgramName": "", "ScheduleEntryType": "FILLER_SLATE"},
            ],
            "NextToken": "t",
        },
        schedule,
    )
    mediatailor.add_response(
        "get_channel_schedule", {"Items": [{"ProgramName": "p2", **_entry()}]}, {**schedule, "NextToken": "t"}
    )
    library_query = {
        "TableName": "library",
        "KeyConditionExpression": ANY,
        "ProjectionExpression": "#n",
        "ExpressionAttributeNames": {"#n": "name"},
    }
    dynamodb.add_response("query", {"Items": [typed({"name": "p2"}), typed({"name": "p3"})]}, library_query)
    mediatailor.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p1"})
    mediatailor.add_client_error("delete_program", **client_error("ConflictException"))
    mediatailor.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p2"})
    mediatailor.add_response("delete_program", {}, {"ChannelName": CHANNEL, "ProgramName": "p3"})
    dynamodb.add_response(
        "delete_item", {}, {"TableName": "library", "Key": {"pk": f"CHANNEL#{CHANNEL}", "sk": "META"}}
    )

    response = handler(cfn_event(LIFECYCLE, "Delete", lifecycle_props(), physical_id=CHANNEL), context)

    assert response == {"PhysicalResourceId": CHANNEL}


def test_lifecycle_delete_of_a_missing_channel_only_forgets_it(
    stub: Stub,
    cfn_event: Event,
    context: Any,
    typed: Callable[..., dict[str, Any]],
    client_error: Callable[..., dict[str, Any]],
) -> None:
    stub("mediatailor").add_client_error("stop_channel", **client_error("NotFoundException", status=404))
    stub("dynamodb").add_response("delete_item", {}, {"TableName": "library", "Key": ANY})

    handler(cfn_event(LIFECYCLE, "Delete", lifecycle_props(), physical_id=CHANNEL), context)


def test_delete_program_gives_up_after_repeated_conflicts(
    stub: Stub, client_error: Callable[..., dict[str, Any]]
) -> None:
    mediatailor = stub("mediatailor")
    for _ in range(channel_lifecycle.CONFLICT_RETRIES):
        mediatailor.add_client_error("delete_program", **client_error("ConflictException"))

    with pytest.raises(Exception, match="ConflictException"):
        channel_lifecycle.delete_program(CHANNEL, "p1")


def test_wait_for_state_times_out(stub: Stub, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 0.0, 1_000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    stub("mediatailor").add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})
    stub("mediatailor").add_response("describe_channel", describe("RUNNING"), {"ChannelName": CHANNEL})

    with pytest.raises(TimeoutError, match="still RUNNING"):
        channel_lifecycle.wait_for_state(CHANNEL, "STOPPED")


def test_unknown_resource_types_fail(cfn_event: Event, context: Any) -> None:
    with pytest.raises(ValueError, match="unsupported resource type"):
        handler(cfn_event("Custom::Other", "Create", {"ChannelName": CHANNEL}), context)


def test_update_expression_aliases_every_attribute() -> None:
    assert update_expression({"name": "x", "owner": "iac"}, ["brief"]) == {
        "UpdateExpression": "SET #a0 = :a0, #a1 = :a1 REMOVE #r0",
        "ExpressionAttributeNames": {"#a0": "name", "#a1": "owner", "#r0": "brief"},
        "ExpressionAttributeValues": {":a0": "x", ":a1": "iac"},
    }


def _entry() -> dict[str, Any]:
    return {
        "Arn": "arn",
        "ChannelName": CHANNEL,
        "SourceLocationName": "fast-media",
        "ScheduleEntryType": "PROGRAM",
        "ApproximateDurationSeconds": 60,
    }
