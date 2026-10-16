import itertools
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from botocore.stub import ANY, Stubber

from fast_channels.functions import autopilot, aws
from fast_channels.functions.settings import settings

RUNTIME = "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore/invocations"
TOKEN_ENDPOINT = "https://fast.auth.us-east-1.amazoncognito.com/oauth2/token"

type Responder = Callable[[httpx.Request], httpx.Response]


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def finished(**outcome: Any) -> dict[str, Any]:
    return {"type": "RUN_FINISHED", "threadId": "t", "runId": "r", **({"outcome": outcome} if outcome else {})}


def text(message: str, message_id: str = "m1") -> dict[str, Any]:
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": message}


def approval(tool: str, interrupt_id: str, plan_id: str = "fast-kids.cafe0001") -> dict[str, Any]:
    arguments = {"plan_id": plan_id} if tool == "schedule___publish" else {}
    value = {"interrupt_id": interrupt_id, "tool": tool, "arguments": arguments, "reason": "", "tool_call_id": "tc"}
    return {"type": "CUSTOM", "name": "approval_required", "value": value}


def context(*remaining_ms: int) -> SimpleNamespace:
    """A Lambda context with ``remaining_ms`` left at successive checks (the last value repeats; 10 min default)."""
    left = itertools.chain(remaining_ms or [600_000], itertools.repeat((remaining_ms or [600_000])[-1]))
    return SimpleNamespace(
        aws_request_id="req-1",
        function_name="autopilot",
        function_version="1",
        invoked_function_arn="arn:aws:lambda:us-east-1:111122223333:function:autopilot",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: next(left),
    )


class Runtime:
    """httpx transport standing in for Cognito's token endpoint and the AgentCore Runtime."""

    def __init__(self, *runs: tuple[int, bytes]) -> None:
        self.runs = list(runs)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            self.token_requests += 1
            assert request.headers["authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600, "token_type": "Bearer"})
        assert str(request.url) == RUNTIME
        self.bodies.append(json.loads(request.content))
        self.headers.append(request.headers)
        status, body = self.runs.pop(0)
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "RUNTIME_INVOKE_URL": RUNTIME,
        "USER_POOL_ID": "us-east-1_pool",
        "AUTOPILOT_CLIENT_ID": "autopilot-client",
        "TOKEN_ENDPOINT": TOKEN_ENDPOINT,
        "SCOPE": "fast-channels/agent.invoke",
        "FAST_PREFIX": "fast",
        "EVENT_BUS": "fast-fast-channels",
        "_X_AMZN_TRACE_ID": "Root=1-abc;Parent=def;Sampled=1",
    }.items():
        monkeypatch.setenv(name, value)
    for cached in (settings, autopilot.autopilot_settings, autopilot._client_secret):
        cached.cache_clear()
    monkeypatch.setattr(autopilot, "_tokens", autopilot.TokenCache())
    yield
    for cached in (settings, autopilot.autopilot_settings, autopilot._client_secret):
        cached.cache_clear()


@pytest.fixture
def cognito() -> Iterator[Stubber]:
    with Stubber(aws.client("cognito-idp")) as stub:
        stub.add_response(
            "describe_user_pool_client",
            {"UserPoolClient": {"ClientId": "autopilot-client", "ClientSecret": "s3cr3t-0123456789abcdefghijklmnop"}},
            {"UserPoolId": "us-east-1_pool", "ClientId": "autopilot-client"},
        )
        yield stub


@pytest.fixture
def events() -> Iterator[list[dict[str, Any]]]:
    published: list[dict[str, Any]] = []
    client = aws.client("events")

    def record(params: dict[str, Any], **_: Any) -> None:
        published.extend(params["Entries"])

    client.meta.events.register("provide-client-params.events.PutEvents", record)
    with Stubber(client) as stub:
        stub.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "e1"}]}, {"Entries": ANY})
        yield published
    client.meta.events.unregister("provide-client-params.events.PutEvents", record)


def use(monkeypatch: pytest.MonkeyPatch, runtime: Responder) -> None:
    client = httpx.Client(transport=httpx.MockTransport(runtime))
    monkeypatch.setattr(autopilot, "http", lambda: client)


def test_session_id_is_hourly_and_padded() -> None:
    session = autopilot.session_id_for("fast-kids", datetime(2026, 10, 16, 7, 59, tzinfo=UTC))
    assert session == "autopilot-fast-kids-20261016T0700"
    assert len(session) == 33
    long = autopilot.session_id_for("fast-a-much-longer-channel-name", datetime(2026, 10, 16, 7, tzinfo=UTC))
    assert long == "autopilot-fast-a-much-longer-channel-name-20261016T07"


def test_sse_events_join_data_lines_and_drop_unterminated_events() -> None:
    lines = [": comment", "event: message", 'data: {"a":', "data: 1}", "", 'data: {"b": 2}', "", 'data: {"c": 3}']
    assert list(autopilot.sse_events(lines)) == [{"a": 1}, {"b": 2}]


def test_only_publish_is_approved() -> None:
    approvals = [approval("schedule___publish", "i1")["value"], approval("x___y", "i2")["value"]]
    entries = autopilot.answers(approvals, channel="fast-kids")
    assert entries == [
        {"interruptId": "i1", "status": "resolved", "payload": {"approved": True}},
        {"interruptId": "i2", "status": "resolved", "payload": {"approved": False, "note": autopilot.DECLINE_NOTE}},
    ]


@pytest.mark.parametrize("plan_id", ["fast-news.cafe0001", "fast-kids-2.cafe0001", "fast-kids", ""])
def test_publish_is_approved_only_for_the_runs_own_channel(plan_id: str) -> None:
    [entry] = autopilot.answers([approval("schedule___publish", "i1", plan_id)["value"]], channel="fast-kids")
    assert entry["payload"]["approved"] is False
    assert "only publish plans for fast-kids" in entry["payload"]["note"]


def test_run_approves_publish_and_reports_the_summary(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    runtime = Runtime(
        (
            200,
            sse(
                {"type": "RUN_STARTED"},
                {"type": "TOOL_CALL_START", "toolCallId": "1", "toolCallName": "schedule___plan"},
                text("Planned 6 h."),
                approval("schedule___publish", "int-1"),
                finished(type="interrupt", interrupts=[{"id": "int-1"}]),
            ),
        ),
        (200, sse({"type": "RUN_STARTED"}, text("Published 6 h, schedule now ends 18:00Z.", "m2"), finished())),
    )
    use(monkeypatch, runtime)

    result = autopilot.handler({"channel": "fast-kids", "horizon_hours": 12}, context())

    assert result["status"] == "SUCCEEDED"
    assert result["summary"] == "Published 6 h, schedule now ends 18:00Z."
    first, resume = runtime.bodies
    assert (
        first["threadId"] == result["session_id"] == runtime.headers[0]["x-amzn-bedrock-agentcore-runtime-session-id"]
    )
    assert first["forwardedProps"] == {"mode": "plan"}
    assert first["state"] == {"channel": "fast-kids"}
    assert "fast-kids" in first["messages"][0]["content"] and "12 hours" in first["messages"][0]["content"]
    assert resume["messages"] == []
    assert resume["resume"] == [{"interruptId": "int-1", "status": "resolved", "payload": {"approved": True}}]
    assert runtime.headers[1]["authorization"] == "Bearer token-1"
    assert runtime.headers[0]["x-amzn-trace-id"] == "Root=1-abc;Parent=def;Sampled=1"
    assert runtime.token_requests == 1
    [entry] = events
    assert (entry["Source"], entry["DetailType"], entry["EventBusName"]) == (
        "fast-channels.fast",
        "Autopilot Run",
        "fast-fast-channels",
    )
    assert json.loads(entry["Detail"])["status"] == "SUCCEEDED"
    cognito.assert_no_pending_responses()


def test_token_is_reused_until_it_expires(monkeypatch: pytest.MonkeyPatch, cognito: Stubber) -> None:
    runtime = Runtime()
    use(monkeypatch, runtime)
    cfg = autopilot.autopilot_settings()
    assert autopilot._tokens.get(cfg) == autopilot._tokens.get(cfg) == "token-1"
    assert runtime.token_requests == 1
    # It is renewed while it still outlives a whole invocation: the Gateway checks it on every tool call of a run.
    assert autopilot._tokens.valid_until - autopilot.time.monotonic() <= 3600 - autopilot.TOKEN_EXPIRY_MARGIN_S
    assert autopilot.TOKEN_EXPIRY_MARGIN_S >= 900
    monkeypatch.setattr(autopilot._tokens, "valid_until", 0.0)
    autopilot._tokens.get(cfg)
    assert runtime.token_requests == 2


def test_run_error_fails_the_invocation(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    body = sse({"type": "RUN_ERROR", "code": "ACCESS_DENIED", "message": "Access denied"})
    use(monkeypatch, Runtime((403, body)))
    with pytest.raises(autopilot.AutopilotError, match="ACCESS_DENIED"):
        autopilot.handler({"channel": "fast-kids"}, context())
    detail = json.loads(events[0]["Detail"])
    assert detail["status"] == "FAILED" and detail["summary"].startswith("ACCESS_DENIED: Access denied")


def test_stream_without_run_finished_fails(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    use(monkeypatch, Runtime((502, b"")))
    with pytest.raises(autopilot.AutopilotError, match="HTTP 502"):
        autopilot.handler({"channel": "fast-kids"}, context())


def test_pending_approvals_stop_when_time_runs_out(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    paused = sse(approval("schedule___publish", "int-1"), finished(type="interrupt", interrupts=[{"id": "int-1"}]))
    use(monkeypatch, Runtime((200, paused)))
    with pytest.raises(autopilot.AutopilotError, match="1 approvals pending"):
        autopilot.handler({"channel": "fast-kids"}, context(600_000, 30_000))
    assert json.loads(events[0]["Detail"])["status"] == "FAILED"


def test_no_run_starts_without_the_time_to_report_it(
    monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]
) -> None:
    runtime = Runtime()
    use(monkeypatch, runtime)
    with pytest.raises(autopilot.AutopilotError, match="stopped after 0 runs"):
        autopilot.handler({"channel": "fast-kids"}, context(autopilot.SAFETY_MARGIN_MS - 1))
    assert json.loads(events[0]["Detail"])["status"] == "FAILED"
    assert runtime.bodies == [] and runtime.token_requests == 0


def test_transport_failures_are_reported_as_failed_runs(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3600})
        raise httpx.ReadTimeout("the stream went silent", request=request)

    use(monkeypatch, unreachable)
    with pytest.raises(autopilot.AutopilotError, match="ReadTimeout: the stream went silent"):
        autopilot.handler({"channel": "fast-kids"}, context())
    detail = json.loads(events[0]["Detail"])
    assert detail["status"] == "FAILED" and detail["summary"].startswith("ReadTimeout")


def test_token_failures_are_reported_as_failed_runs(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    use(monkeypatch, lambda request: httpx.Response(500, request=request))
    with pytest.raises(autopilot.AutopilotError, match="HTTPStatusError"):
        autopilot.handler({"channel": "fast-kids"}, context())
    assert json.loads(events[0]["Detail"])["status"] == "FAILED"


def test_a_run_gets_only_the_time_the_invocation_has_left(
    monkeypatch: pytest.MonkeyPatch, cognito: Stubber, events: list[dict[str, Any]]
) -> None:
    clock = itertools.count(0, 50)  # seconds: every event arrives 50 s after the last
    monkeypatch.setattr(autopilot.time, "monotonic", lambda: float(next(clock)))
    long_run = sse({"type": "RUN_STARTED"}, text("Planning"), text(" still"), finished())
    use(monkeypatch, Runtime((200, long_run)))
    with pytest.raises(autopilot.AutopilotError, match="still going after 70 s"):
        autopilot.handler({"channel": "fast-kids"}, context(autopilot.REPORT_MARGIN_MS + 70_000))
    assert json.loads(events[0]["Detail"])["status"] == "FAILED"


def test_missing_configuration_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPE")
    with pytest.raises(RuntimeError, match="SCOPE"):
        autopilot.AutopilotSettings.from_env()
