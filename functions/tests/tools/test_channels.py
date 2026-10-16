import json
import time
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from fast_channels.core import specs
from fast_channels.core.ladder import Ladder
from fast_channels.core.profiles import profile
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _library_store as store
from fast_channels.functions.tools import channels
from fast_channels.functions.tools.registry import ToolContext, ToolError
from fast_channels.functions.tools.schedule import ChannelInput

TABLE, BUCKET = "library-test", "media-test"
ACCOUNT = "111122223333"
STANDARD = Ladder.builtin("standard")
LV = STANDARD.version
CA_HOST = "https://e4134234413166c2d978e6bbd36eb21f.7wzuvg.channel-assembly.mediatailor.us-east-1.amazonaws.com"
MT_HOST = "https://8d3d0b2de74c42869dc871421200f174.mediatailor.us-east-1.amazonaws.com"
HLS_PREFIX = f"{MT_HOST}/v1/master/1bab6b42/fast-demo/"
DASH_PREFIX = f"{MT_HOST}/v1/dash/1bab6b42/fast-demo/"
DESTINATION = f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-destination:fast-mediatailor"
SCHEDULER_ROLE = f"arn:aws:iam::{ACCOUNT}:role/fast-scheduler"
TEMPLATE = {
    "content_base_url": "https://media.example.com",
    "log_percent": 100,
    "ads_url": None,
    "log_destination_arn": DESTINATION,
    "log_types": {"AD_DECISION_SERVER_LOGS": "ads", "MANIFEST_SERVICE_LOGS": "manifest", "TRANSCODE_LOGS": "transcode"},
}
CTX = ToolContext(tool="channels___create_channel", correlation_id="fast-console-session-1")
_typed = TypeSerializer().serialize


def decimals(value: Any) -> Any:
    match value:
        case float():
            return Decimal(str(value))
        case dict():
            return {k: decimals(v) for k, v in value.items()}
        case list():
            return [decimals(v) for v in value]
        case _:
            return value


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _typed(decimals(v)) for k, v in item.items()}


@dataclass
class Calls:
    params: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def __call__(self, params: dict[str, Any], event_name: str, **_: Any) -> None:
        self.params.append((event_name.rsplit(".", 1)[-1], params))

    def of(self, operation: str) -> list[dict[str, Any]]:
        return [p for name, p in self.params if name == operation]

    def names(self) -> list[str]:
        return [name for name, _ in self.params]


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "FAST_PREFIX": "fast",
        "LIBRARY_TABLE": TABLE,
        "MEDIA_BUCKET": BUCKET,
        "SOURCE_LOCATION": "fast-origin",
        "CDN_DOMAIN": "d111111abcdef8.cloudfront.net",
        "PACKAGE_PROFILES": "hls,hls-avc,hls-hevc,hls-av1,dash-avc",
        "DEFAULT_LADDER_VERSION": LV,
        "PLAYBACK_CONFIG_TEMPLATE": json.dumps(TEMPLATE),
        "AUTO_STOP_HOURS": "4",
        "MAX_AGENT_CHANNELS": "2",
        "SCHEDULER_GROUP": "fast-channels",
        "SCHEDULER_ROLE_ARN": SCHEDULER_ROLE,
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def aws_stubs() -> Iterator[SimpleNamespace]:
    clients = {
        "ddb": aws.table(TABLE).meta.client,
        "mt": aws.client("mediatailor"),
        "scheduler": aws.client("scheduler"),
        "logs": aws.client("logs"),
        "s3": aws.client("s3"),
    }
    calls = Calls()
    with ExitStack() as stack:
        stubs = {}
        for name, client in clients.items():
            client.meta.events.register("provide-client-params", calls)
            stack.callback(client.meta.events.unregister, "provide-client-params", calls)
            stubs[name] = stack.enter_context(Stubber(client))
        yield SimpleNamespace(**stubs, calls=calls)
        for stub in stubs.values():
            stub.assert_no_pending_responses()


def channel(name: str = "fast-demo", **changes: Any) -> store.ChannelRecord:
    fields: dict[str, Any] = {
        "name": name,
        "ladder_version": LV,
        "outputs": ["hls", "dash-avc"],
        "owner": "iac",
        "tier": "BASIC",
        "arn": f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:channel/{name}",
        "playback_configuration_name": name,
        "hls_prefix": HLS_PREFIX,
        "dash_prefix": DASH_PREFIX,
    }
    return store.ChannelRecord(**fields | changes)


def ladder(status: str = "READY") -> store.LadderRecord:
    kit = store.LadderKit(filler="fast-filler", break_slates={30: "fast-slate-30"})
    return store.LadderRecord(ladder_version=LV, status=status, kit=kit)


def get_item(stub: Stubber, key: dict[str, str], item: dict[str, Any] | None = None) -> None:
    stub.add_response("get_item", {"Item": dynamo(item)} if item else {}, {"TableName": TABLE, "Key": key})


def query(stub: Stubber, *items: dict[str, Any]) -> None:
    stub.add_response("query", {"Items": [dynamo(i) for i in items], "Count": len(items), "ScannedCount": len(items)})


def described(name: str, state: str) -> dict[str, Any]:
    return {
        "Arn": f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:channel/{name}",
        "ChannelName": name,
        "ChannelState": state,
        "Outputs": [
            {"ManifestName": o, "PlaybackUrl": f"{CA_HOST}/v1/channel/{name}/{o}.m3u8", "SourceGroup": o}
            for o in ("hls", "dash-avc")
        ],
        "PlaybackMode": "LINEAR",
        "Tier": "BASIC",
        "LogConfiguration": {"LogTypes": ["AS_RUN"]},
    }


def no_schedule(stub: Stubber, channel_name: str) -> None:
    stub.add_client_error(
        "get_schedule",
        "ResourceNotFoundException",
        expected_params={"Name": f"{channel_name}-auto-stop", "GroupName": "fast-channels"},
    )


# --- reads ----------------------------------------------------------------------------------------------------------


def test_list_channels_merges_the_library_with_live_state(aws_stubs: SimpleNamespace) -> None:
    query(aws_stubs.ddb, channel().to_item(), channel("fast-agent-kids", owner="agent").to_item())
    page = {
        "Items": [
            {**described("fast-demo", "RUNNING"), "ChannelState": "RUNNING"},
            {**described("other-team", "STOPPED")},
        ]
    }
    aws_stubs.mt.add_response("list_channels", page, {})

    result = channels.list_channels(channels.ListChannelsInput(), CTX)

    assert [(c.name, c.state, c.owner) for c in result.channels] == [
        ("fast-demo", "RUNNING", store.Owner.IAC),
        ("fast-agent-kids", "MISSING", store.Owner.AGENT),
    ]
    assert result.model_dump(mode="json")["channels"][0] == {
        "name": "fast-demo",
        "state": "RUNNING",
        "owner": "iac",
        "ladder_version": LV,
        "outputs": ["hls", "dash-avc"],
        "tier": "BASIC",
    }


def test_get_channel_returns_playback_urls_per_output(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    aws_stubs.mt.add_response("describe_channel", described("fast-demo", "RUNNING"), {"ChannelName": "fast-demo"})
    aws_stubs.scheduler.add_response(
        "get_schedule",
        {"Name": "fast-demo-auto-stop", "ScheduleExpression": "at(2026-10-16T09:20:00)"},
        {"Name": "fast-demo-auto-stop", "GroupName": "fast-channels"},
    )

    result = channels.get_channel(ChannelInput(channel="fast-demo"), CTX)

    cdn = "https://d111111abcdef8.cloudfront.net"
    assert result.playback == {
        "hls": {
            "ssai": f"{cdn}/v1/master/1bab6b42/fast-demo/hls.m3u8",
            "sgai": f"{cdn}/v1/master/1bab6b42/fast-demo/hls.m3u8?aws.insertionMode=GUIDED",
        },
        "dash-avc": {"dash": f"{cdn}/v1/dash/1bab6b42/fast-demo/dash-avc.mpd"},
    }
    assert (result.state, result.auto_stop_at, result.arn) == (
        "RUNNING",
        "2026-10-16T09:20:00Z",
        described("fast-demo", "RUNNING")["Arn"],
    )


def test_get_channel_reads_prefixes_from_the_playback_configuration(aws_stubs: SimpleNamespace) -> None:
    record = channel(hls_prefix=None, dash_prefix=None)
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), record.to_item())
    aws_stubs.mt.add_response("describe_channel", described("fast-demo", "STOPPED"), {"ChannelName": "fast-demo"})
    aws_stubs.mt.add_response(
        "get_playback_configuration",
        {
            "HlsConfiguration": {"ManifestEndpointPrefix": HLS_PREFIX},
            "DashConfiguration": {"ManifestEndpointPrefix": DASH_PREFIX},
        },
        {"Name": "fast-demo"},
    )
    no_schedule(aws_stubs.scheduler, "fast-demo")

    result = channels.get_channel(ChannelInput(channel="fast-demo"), CTX)

    assert set(result.playback) == {"hls", "dash-avc"} and result.auto_stop_at is None


def test_get_channel_without_live_channel_or_playback_configuration(
    aws_stubs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCHEDULER_GROUP")
    settings.cache_clear()
    record = channel(hls_prefix=None, dash_prefix=None)
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), record.to_item())
    aws_stubs.mt.add_client_error("describe_channel", "NotFoundException", http_status_code=404)
    aws_stubs.mt.add_client_error("get_playback_configuration", "NotFoundException", http_status_code=404)

    result = channels.get_channel(ChannelInput(channel="fast-demo"), CTX)

    assert (result.state, result.playback, result.auto_stop_at) == ("MISSING", {}, None)
    assert result.arn == record.arn


# --- start / stop ---------------------------------------------------------------------------------------------------


def test_start_channel_schedules_the_auto_stop(aws_stubs: SimpleNamespace) -> None:
    mt, scheduler = aws_stubs.mt, aws_stubs.scheduler
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    mt.add_response("describe_channel", described("fast-demo", "STOPPED"), {"ChannelName": "fast-demo"})
    mt.add_response("start_channel", {}, {"ChannelName": "fast-demo"})
    mt.add_response("describe_channel", described("fast-demo", "RUNNING"), {"ChannelName": "fast-demo"})
    scheduler.add_client_error("create_schedule", "ConflictException", "Schedule fast-demo-auto-stop already exists.")
    scheduler.add_response("update_schedule", {"ScheduleArn": "arn:aws:scheduler:us-east-1:1:schedule/g/s"})
    mt.add_response("list_alerts", {"Items": []}, {"ResourceArn": described("fast-demo", "RUNNING")["Arn"]})

    before = datetime.now(UTC)
    result = channels.start_channel(ChannelInput(channel="fast-demo"), CTX)

    [created], [request] = aws_stubs.calls.of("CreateSchedule"), aws_stubs.calls.of("UpdateSchedule")
    assert {k: v for k, v in created.items() if k != "ClientToken"} == {
        k: v for k, v in request.items() if k != "ClientToken"
    }
    assert request["Target"] == {
        "Arn": "arn:aws:scheduler:::aws-sdk:mediatailor:stopChannel",
        "RoleArn": SCHEDULER_ROLE,
        "Input": '{"ChannelName": "fast-demo"}',
        "RetryPolicy": {"MaximumRetryAttempts": 5, "MaximumEventAgeInSeconds": 3600},
    }
    assert (request["GroupName"], request["ActionAfterCompletion"], request["ScheduleExpressionTimezone"]) == (
        "fast-channels",
        "DELETE",
        "UTC",
    )
    stop_at = datetime.fromisoformat(request["ScheduleExpression"].removeprefix("at(").removesuffix(")"))
    assert abs(stop_at.replace(tzinfo=UTC) - before - timedelta(hours=4)) < timedelta(seconds=5)
    assert result.state == "RUNNING" and result.auto_stop_at == f"{stop_at:%Y-%m-%dT%H:%M:%S}Z"


def test_start_channel_without_auto_stop_leaves_a_running_channel_alone(
    aws_stubs: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTO_STOP_HOURS", "0")
    settings.cache_clear()
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    aws_stubs.mt.add_response("describe_channel", described("fast-demo", "RUNNING"), {"ChannelName": "fast-demo"})
    aws_stubs.mt.add_response("list_alerts", {"Items": []}, {"ResourceArn": ANY})
    result = channels.start_channel(ChannelInput(channel="fast-demo"), CTX)
    assert result.auto_stop_at is None and "StartChannel" not in aws_stubs.calls.names()


def test_start_channel_missing_in_channel_assembly(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    aws_stubs.mt.add_client_error("describe_channel", "NotFoundException", http_status_code=404)
    aws_stubs.mt.add_client_error("start_channel", "NotFoundException", http_status_code=404)
    with pytest.raises(ClientError, match="NotFoundException"):  # the dispatcher reports NOT_FOUND
        channels.start_channel(ChannelInput(channel="fast-demo"), CTX)


def test_stop_channel_cancels_the_auto_stop(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    aws_stubs.mt.add_response("stop_channel", {}, {"ChannelName": "fast-demo"})
    aws_stubs.mt.add_response("describe_channel", described("fast-demo", "STOPPED"), {"ChannelName": "fast-demo"})
    aws_stubs.scheduler.add_client_error("delete_schedule", "ResourceNotFoundException")
    result = channels.stop_channel(ChannelInput(channel="fast-demo"), CTX)
    assert (result.channel, result.state) == ("fast-demo", "STOPPED")


def test_stop_channel_missing_in_channel_assembly(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    aws_stubs.mt.add_client_error("stop_channel", "NotFoundException", http_status_code=404)
    with pytest.raises(ToolError) as raised:
        channels.stop_channel(ChannelInput(channel="fast-demo"), CTX)
    assert raised.value.code == "NOT_FOUND"


# --- create ---------------------------------------------------------------------------------------------------------

AGENT = "fast-agent-kids"
PC_ARN = f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:playbackConfiguration/{AGENT}"


def create_preamble(aws_stubs: SimpleNamespace, *, agent_channels: int = 1) -> None:
    ddb = aws_stubs.ddb
    get_item(ddb, store.ladder_key(LV), ladder().to_item())
    get_item(ddb, store.channel_key(AGENT))
    query(ddb, *(channel(f"fast-agent-{i}", owner="agent").to_item() for i in range(agent_channels)))


def created_channel() -> dict[str, Any]:
    return {k: v for k, v in described(AGENT, "STOPPED").items() if k != "LogConfiguration"}


def playback_configuration() -> dict[str, Any]:
    return {
        "Name": AGENT,
        "PlaybackConfigurationArn": PC_ARN,
        "HlsConfiguration": {"ManifestEndpointPrefix": f"{MT_HOST}/v1/master/1bab6b42/{AGENT}/"},
        "DashConfiguration": {"ManifestEndpointPrefix": f"{MT_HOST}/v1/dash/1bab6b42/{AGENT}/"},
    }


def test_create_channel_assembles_channel_playback_logs_and_policy(aws_stubs: SimpleNamespace) -> None:
    create_preamble(aws_stubs)
    mt, logs = aws_stubs.mt, aws_stubs.logs
    mt.add_response("create_channel", created_channel())
    mt.add_response("configure_logs_for_channel", {}, {"ChannelName": AGENT, "LogTypes": ["AS_RUN"]})
    mt.add_response("put_playback_configuration", playback_configuration())
    mt.add_response(
        "configure_logs_for_playback_configuration", {"PercentEnabled": 100, "PlaybackConfigurationName": AGENT}
    )
    for suffix in ("ads", "manifest", "transcode"):
        logs.add_response("put_delivery_source", {})
        logs.add_response("create_delivery", {"delivery": {"id": f"d-{suffix}"}})
    mt.add_response("put_channel_policy", {})
    aws_stubs.ddb.add_response("put_item", {})
    no_schedule(aws_stubs.scheduler, AGENT)

    payload = channels.CreateChannelInput(name="fast-agent-kids", outputs=["hls", "dash-avc"])
    result = channels.create_channel(payload, CTX)

    tags = {"fast:app": "fast", "fast:owner": "agent", "fast:ladder": LV, "fast:correlation-id": CTX.correlation_id}
    [create] = aws_stubs.calls.of("CreateChannel")
    assert create == {
        "ChannelName": AGENT,
        "PlaybackMode": "LINEAR",
        "Tier": "BASIC",
        "Outputs": specs.channel_outputs([profile("hls"), profile("dash-avc")]),
        "FillerSlate": {"SourceLocationName": "fast-origin", "VodSourceName": "fast-filler"},
        "Tags": tags,
    }
    [put] = aws_stubs.calls.of("PutPlaybackConfiguration")
    assert put["VideoContentSourceUrl"] == f"{CA_HOST}/v1/channel/{AGENT}/"
    assert put["AdDecisionServerUrl"] == specs.ads_url(
        content_base_url="https://media.example.com", ladder_version=LV, channel=AGENT
    )
    assert "LogConfiguration" not in put and put["Tags"] == tags
    assert aws_stubs.calls.of("ConfigureLogsForPlaybackConfiguration")[0]["EnabledLoggingStrategies"] == ["VENDED_LOGS"]
    sources = aws_stubs.calls.of("PutDeliverySource")
    assert [(s["name"], s["logType"], s["resourceArn"]) for s in sources] == [
        (f"{AGENT}-ads", "AD_DECISION_SERVER_LOGS", PC_ARN),
        (f"{AGENT}-manifest", "MANIFEST_SERVICE_LOGS", PC_ARN),
        (f"{AGENT}-transcode", "TRANSCODE_LOGS", PC_ARN),
    ]
    assert {d["deliveryDestinationArn"] for d in aws_stubs.calls.of("CreateDelivery")} == {DESTINATION}
    policy = json.loads(aws_stubs.calls.of("PutChannelPolicy")[0]["Policy"])
    assert policy["Statement"][0]["Condition"] == {
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
        "ArnLike": {"aws:SourceArn": PC_ARN},
    }
    [record] = aws_stubs.calls.of("PutItem")
    assert record["Item"]["owner"] == "agent" and record["Item"]["gsi1sk"] == f"agent#{AGENT}"
    assert "ConditionExpression" in record
    assert result.name == AGENT and result.state == "STOPPED"
    assert (
        result.playback["hls"]["ssai"] == f"https://d111111abcdef8.cloudfront.net/v1/master/1bab6b42/{AGENT}/hls.m3u8"
    )


def test_create_channel_rolls_back_when_a_step_fails(aws_stubs: SimpleNamespace) -> None:
    create_preamble(aws_stubs)
    mt, logs = aws_stubs.mt, aws_stubs.logs
    mt.add_response("create_channel", created_channel())
    mt.add_response("configure_logs_for_channel", {})
    mt.add_response("put_playback_configuration", playback_configuration())
    mt.add_response(
        "configure_logs_for_playback_configuration", {"PercentEnabled": 100, "PlaybackConfigurationName": AGENT}
    )
    logs.add_response("put_delivery_source", {})
    logs.add_response("create_delivery", {"delivery": {"id": "d-ads"}})
    logs.add_client_error("put_delivery_source", "AccessDeniedException")
    logs.add_response("delete_delivery", {}, {"id": "d-ads"})
    logs.add_client_error("delete_delivery_source", "ConflictException")  # logged, the rollback continues
    mt.add_response("delete_playback_configuration", {}, {"Name": AGENT})
    mt.add_response("delete_channel", {}, {"ChannelName": AGENT})

    with pytest.raises(ClientError, match="AccessDenied"):
        channels.create_channel(channels.CreateChannelInput(name="kids"), CTX)
    assert aws_stubs.calls.names()[-4:] == [
        "DeleteDelivery",
        "DeleteDeliverySource",
        "DeletePlaybackConfiguration",
        "DeleteChannel",
    ]


def test_create_channel_enforces_the_agent_channel_quota(aws_stubs: SimpleNamespace) -> None:
    create_preamble(aws_stubs, agent_channels=2)
    with pytest.raises(ToolError) as raised:
        channels.create_channel(channels.CreateChannelInput(name="kids"), CTX)
    assert raised.value.code == "QUOTA"


def test_create_channel_refuses_an_existing_name(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.ladder_key(LV), ladder().to_item())
    get_item(aws_stubs.ddb, store.channel_key(AGENT), channel(AGENT, owner="agent").to_item())
    with pytest.raises(ToolError) as raised:
        channels.create_channel(channels.CreateChannelInput(name="kids"), CTX)
    assert raised.value.code == "CONFLICT"


def test_create_channel_needs_a_ready_ladder_and_known_outputs(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.ladder_key(LV), ladder("PROVISIONING").to_item())
    with pytest.raises(ToolError, match="PROVISIONING") as raised:
        channels.create_channel(channels.CreateChannelInput(name="kids"), CTX)
    assert raised.value.code == "VALIDATION"

    get_item(aws_stubs.ddb, store.ladder_key(LV), ladder().to_item())
    with pytest.raises(ToolError, match="hls-uhd") as raised:
        channels.create_channel(channels.CreateChannelInput(name="kids", outputs=["hls", "hls-uhd"]), CTX)
    assert raised.value.code == "VALIDATION"


# --- delete ---------------------------------------------------------------------------------------------------------


def test_delete_channel_refuses_deployment_channels(aws_stubs: SimpleNamespace) -> None:
    get_item(aws_stubs.ddb, store.channel_key("fast-demo"), channel().to_item())
    with pytest.raises(ToolError) as raised:
        channels.delete_channel(ChannelInput(channel="fast-demo"), CTX)
    assert raised.value.code == "FORBIDDEN"


def test_delete_channel_tears_down_in_reverse_order(aws_stubs: SimpleNamespace) -> None:
    ddb, mt, logs = aws_stubs.ddb, aws_stubs.mt, aws_stubs.logs
    record = channel(AGENT, owner="agent")
    get_item(ddb, store.channel_key(AGENT), record.to_item())
    mt.add_response("stop_channel", {}, {"ChannelName": AGENT})
    mt.add_response("describe_channel", described(AGENT, "STOPPED"), {"ChannelName": AGENT})
    entry = {"Arn": "a", "ChannelName": AGENT, "ProgramName": "p1", "SourceLocationName": "fast-origin"}
    mt.add_response("get_channel_schedule", {"Items": [entry]})
    query(ddb, {"pk": f"CHANNEL#{AGENT}", "sk": "PROGRAM#0001792152000000", "name": "p2"})
    for name in ("p1", "p2"):
        mt.add_response("delete_program", {}, {"ChannelName": AGENT, "ProgramName": name})
    aws_stubs.scheduler.add_response(
        "delete_schedule", {}, {"Name": f"{AGENT}-auto-stop", "GroupName": "fast-channels"}
    )
    mt.add_response("delete_channel_policy", {}, {"ChannelName": AGENT})
    deliveries = [{"id": f"d-{s}", "deliverySourceName": f"{AGENT}-{s}"} for s in ("ads", "manifest", "transcode")]
    logs.add_response("describe_deliveries", {"deliveries": [*deliveries, {"id": "x", "deliverySourceName": "other"}]})
    for delivery in deliveries:
        logs.add_response("delete_delivery", {}, {"id": delivery["id"]})
    for suffix in ("ads", "manifest", "transcode"):
        logs.add_response("delete_delivery_source", {}, {"name": f"{AGENT}-{suffix}"})
    mt.add_response("delete_playback_configuration", {}, {"Name": AGENT})
    mt.add_response("delete_channel", {}, {"ChannelName": AGENT})
    query(ddb, record.to_item(), {"pk": f"CHANNEL#{AGENT}", "sk": "LEASE"})
    ddb.add_response("batch_write_item", {"UnprocessedItems": {}})
    aws_stubs.s3.add_response("delete_objects", {})

    result = channels.delete_channel(ChannelInput(channel=AGENT), CTX)

    assert (result.channel, result.state, result.programs_deleted) == (AGENT, "DELETED", 2)
    [batch] = aws_stubs.calls.of("BatchWriteItem")
    assert [r["DeleteRequest"]["Key"]["sk"] for r in batch["RequestItems"][TABLE]] == ["META", "LEASE"]
