"""The agent runner: approvals answered from the envelope and its caps, every call ledgered, refusals escalated."""

import dataclasses
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.agents.envelope import Envelope, Refusal, Role
from fast_channels.functions import aws
from fast_channels.functions.agents import envelope_record, runner
from fast_channels.functions.autopilot import AutopilotSettings, RunOutcome
from fast_channels.functions.settings import settings as function_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agent_tables import AgentTables

ENVELOPE = Envelope(always_on=("fast-demo",)).resolved("fast")
TOPIC = "arn:aws:sns:us-east-1:123456789012:fast-alerts"
START = "channels___start_channel"


@dataclass
class Context:
    left_ms: int = 600_000
    aws_request_id: str = "request-1"
    function_name: str = "fast-agent-operations"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fast-agent-operations"
    log_group_name: str = "/aws/lambda/fast-agent-operations"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return self.left_ms


@dataclass
class Topic:
    published: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, **message: Any) -> dict[str, str]:
        self.published.append(message)
        return {"MessageId": "1"}


def settings(tables: AgentTables, role: Role = Role.OPERATIONS) -> runner.RunnerSettings:
    return runner.RunnerSettings(
        role=role,
        transport=AutopilotSettings(
            runtime_invoke_url="https://runtime.example/invocations",
            user_pool_id="us-east-1_pool",
            client_id="agent-client",
            token_endpoint="https://auth.example/oauth2/token",
            scope="fast-channels/agent.invoke",
        ),
        envelope=ENVELOPE,
        table=tables.agents,
        ledger_table=tables.ledger,
        alert_topic=TOPIC,
        prefix="fast",
    )


def events(*items: dict[str, Any]) -> RunOutcome:
    outcome = RunOutcome()
    for item in items:
        outcome.apply(item)
    return outcome


def started(call: str, tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "TOOL_CALL_START", "toolCallId": call, "toolCallName": tool},
        {"type": "TOOL_CALL_ARGS", "toolCallId": call, "delta": json.dumps(arguments)},
    ]


def approval(call: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    value = {"interrupt_id": f"i-{call}", "tool": tool, "arguments": arguments, "tool_call_id": call}
    return {"type": "CUSTOM", "name": runner.APPROVAL_EVENT, "value": value}


def result(call: str, content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "TOOL_CALL_RESULT", "toolCallId": call, "content": json.dumps(content)}


def said(text: str) -> dict[str, Any]:
    return {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": text}


FINISHED: dict[str, Any] = {"type": "RUN_FINISHED"}


@pytest.fixture
def stage(agent_tables: AgentTables, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentTables, Topic]]:
    topic = Topic()
    monkeypatch.setattr(aws, "client", lambda service: topic if service == "sns" else agent_tables.client)
    monkeypatch.setattr(aws, "table", agent_tables.table)
    monkeypatch.setenv("LEDGER_TABLE", agent_tables.ledger)
    yield agent_tables, topic


def scripted(monkeypatch: pytest.MonkeyPatch, runs: list[RunOutcome]) -> list[dict[str, Any]]:
    """Replace the Runtime with ``runs``, one per AG-UI run; returns each run's ``resume``."""
    resumes: list[dict[str, Any]] = []
    remaining = list(runs)

    def run_once(cfg: AutopilotSettings, **kwargs: Any) -> RunOutcome:
        assert cfg.client_id == "agent-client"
        resumes.append({"prompt": kwargs["prompt"], "resume": kwargs["resume"]})
        return remaining.pop(0)

    monkeypatch.setattr(runner, "run_once", run_once)
    return resumes


def handle(
    tables: AgentTables, monkeypatch: pytest.MonkeyPatch, request: runner.RunRequest, role: Role = Role.OPERATIONS
) -> dict[str, Any]:
    monkeypatch.setattr(runner, "runner_settings", lambda: settings(tables, role))
    return runner.handler(request.model_dump(mode="json"), Context())


def request(correlation: str = "census-1") -> runner.RunRequest:
    return runner.RunRequest(
        prompt="The always-on channel fast-demo stopped.",
        channel="fast-demo",
        correlation_id=correlation,
        facts={"channel": "fast-demo", "state": "STOPPED"},
    )


def ledger(tables: AgentTables) -> list[tuple[str, str, dict[str, Any]]]:
    entries = sorted(tables.ledger_entries(), key=lambda e: e["sk"])
    return [(e["action"], e["principal"], json.loads(e["detail"])) for e in entries]


def test_a_restart_inside_the_envelope_is_approved_and_every_call_ledgered(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    resumes = scripted(
        monkeypatch,
        [
            events(
                *started("c0", "channels___get_channel", {"channel": "fast-demo"}),
                result("c0", {"channel": "fast-demo", "state": "STOPPED"}),
                *started("c1", START, {"channel": "fast-demo"}),
                approval("c1", START, {"channel": "fast-demo"}),
                FINISHED,
            ),
            events(result("c1", {"channel": "fast-demo", "state": "RUNNING"}), said("Restarted fast-demo."), FINISHED),
        ],
    )

    found = handle(tables, monkeypatch, request())

    assert found["approved"] == [START] and not found["refused"] and not found["escalated"]
    assert found["calls"] == [("channels___get_channel", "done"), (START, "done")]
    assert resumes[1]["resume"] == [{"interruptId": "i-c1", "status": "resolved", "payload": {"approved": True}}]
    assert resumes[1]["prompt"] is None
    recorded = ledger(tables)
    assert [(action, principal) for action, principal, _ in recorded] == [
        ("agent.tool", "agent-operations"),
        ("agent.approve", "agent-operations"),
        ("agent.tool", "agent-operations"),
    ]
    assert all(detail["envelope"] == ENVELOPE.version for _, _, detail in recorded)
    assert recorded[1][2]["channel"] == "fast-demo" and recorded[2][2]["outcome"] == "done"
    assert not topic.published
    window = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
    assert tables.counts()[f"CAP#operations#{START}#fast-demo@{window}"] == 1


def test_a_second_restart_within_the_hour_is_refused_and_escalated(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    for correlation in ("census-1", "census-2"):
        scripted(
            monkeypatch,
            [
                events(
                    *started("c1", START, {"channel": "fast-demo"}),
                    approval("c1", START, {"channel": "fast-demo"}),
                    FINISHED,
                ),
                events(result("c1", {"ok": False, "error": {"code": "DECLINED"}}), said("Needs a human."), FINISHED),
            ],
        )
        found = handle(tables, monkeypatch, request(correlation))

    assert found["approved"] == [] and found["escalated"]
    assert "cap of 1 an hour" in found["refused"][0]
    [message] = topic.published
    assert message["TopicArn"] == TOPIC and "operations agent needs a human" in message["Subject"]
    assert "cap of 1 an hour" in message["Message"] and "Needs a human." in message["Message"]
    actions = [action for action, _, _ in ledger(tables)]
    assert actions.count("agent.approve") == 1 and actions.count("agent.refuse") == 1
    assert actions[-1] == "agent.escalate"
    [refusal] = [detail for action, _, detail in ledger(tables) if action == "agent.refuse"]
    assert refusal["refusal"] == Refusal.CAP.value


@pytest.mark.parametrize(
    ("tool", "arguments", "refusal"),
    [
        ("channels___stop_channel", {"channel": "fast-demo"}, Refusal.NEEDS_HUMAN),
        (START, {"channel": "fast-news"}, Refusal.OUT_OF_SCOPE),
        ("schedule___publish", {"plan_id": "fast-demo.abc"}, Refusal.NOT_GRANTED),
    ],
)
def test_an_action_outside_the_envelope_is_refused_and_escalated(
    stage: tuple[AgentTables, Topic],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, Any],
    refusal: Refusal,
) -> None:
    tables, topic = stage
    resumes = scripted(
        monkeypatch,
        [
            events(*started("c1", tool, arguments), approval("c1", tool, arguments), FINISHED),
            events(result("c1", {"ok": False, "error": {"code": "DECLINED"}}), said("Left for an operator."), FINISHED),
        ],
    )

    found = handle(tables, monkeypatch, request())

    assert found["escalated"] and found["calls"] == [(tool, "declined")]
    [entry] = resumes[1]["resume"]
    assert entry["payload"]["approved"] is False and "Outside your autonomy envelope" in entry["payload"]["note"]
    [refused] = [detail for action, _, detail in ledger(tables) if action == "agent.refuse"]
    assert refused["refusal"] == refusal.value
    assert len(topic.published) == 1
    assert tables.counts() == {}, "a refused action takes no cap"


def test_a_call_the_gateway_denied_is_escalated(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    denied = {"ok": False, "error": {"code": "TOOL_ERROR", "message": "Tool call not authorized by the policy engine"}}
    scripted(
        monkeypatch,
        [events(*started("c1", "schedule___plan", {"channel": "fast-demo"}), result("c1", denied), FINISHED)],
    )

    found = handle(tables, monkeypatch, request())

    assert found["calls"] == [("schedule___plan", "refused")] and found["escalated"]
    assert "schedule___plan: denied by the Gateway" in topic.published[0]["Message"]


def test_a_failed_run_is_escalated_with_its_error(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    monkeypatch.setattr(runner, "run_once", lambda cfg, **kwargs: (_ for _ in ()).throw(RuntimeError("Runtime 503")))

    found = handle(tables, monkeypatch, request())

    assert found["error"] == "RuntimeError: Runtime 503" and found["escalated"]
    assert "Runtime 503" in topic.published[0]["Message"]
    assert [action for action, _, _ in ledger(tables)] == ["agent.escalate"]


def test_two_concurrent_restarts_approve_exactly_one(stage: tuple[AgentTables, Topic]) -> None:
    tables, _ = stage
    cfg = settings(tables)
    barrier = threading.Barrier(2)
    decisions: list[runner.Decision] = []
    lock = threading.Lock()
    now = datetime.now(UTC)

    def restart() -> None:
        barrier.wait()
        decision = runner.decide(cfg, {"tool": START, "arguments": {"channel": "fast-demo"}}, now=now)
        with lock:
            decisions.append(decision)

    threads = [threading.Thread(target=restart) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(d.approved for d in decisions) == [False, True]
    assert [d.refusal for d in decisions if not d.approved] == [Refusal.CAP]


def test_the_sales_agent_only_proposes(stage: tuple[AgentTables, Topic]) -> None:
    tables, _ = stage
    cfg = settings(tables, Role.SALES)

    decision = runner.decide(cfg, {"tool": START, "arguments": {"channel": "fast-demo"}}, now=datetime.now(UTC))

    assert not decision.approved and decision.refusal is Refusal.DISABLED


def test_a_retried_trigger_continues_its_conversation() -> None:
    first, again = (runner.session_id_for(Role.OPERATIONS, "census-1") for _ in range(2))

    assert first == again and first.startswith("agent-operations-") and len(first) >= 33
    assert runner.session_id_for(Role.OPERATIONS, "census-2") != first


def test_the_envelope_record_appends_its_version(agent_tables: AgentTables, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aws, "client", lambda service: agent_tables.client)
    monkeypatch.setattr(aws, "table", agent_tables.table)
    monkeypatch.setenv("LEDGER_TABLE", agent_tables.ledger)
    monkeypatch.setenv("ENVELOPE", ENVELOPE.document)
    monkeypatch.setenv("FAST_PREFIX", "fast")
    function_settings.cache_clear()

    found = envelope_record.handler({}, Context())

    [entry] = agent_tables.ledger_entries()
    assert found["version"] == ENVELOPE.version
    assert (entry["pk"], entry["action"], entry["kind"], entry["principal"]) == (
        "agents/envelope",
        "envelope.apply",
        "envelope",
        "deploy",
    )
    assert json.loads(entry["detail"])["agents"] == ["operations"]
    function_settings.cache_clear()


@dataclass
class MediaTailor:
    states: dict[str, str]

    def describe_channel(self, *, ChannelName: str) -> dict[str, str]:  # noqa: N803
        return {"ChannelState": self.states[ChannelName]}


def census(running: int, previous: int = 1) -> dict[str, Any]:
    return {
        "id": "event-1",
        "detail-type": "Census Changed",
        "source": "fast-channels.fast",
        "time": "2026-11-07T10:00:00Z",
        "detail": {"running": running, "previous": previous, "correlation_id": "census-execution"},
    }


def off_air(state: str) -> dict[str, Any]:
    return {
        "id": "event-2",
        "detail-type": "CloudWatch Alarm State Change",
        "source": "aws.cloudwatch",
        "detail": {"alarmName": "fast-probe-off-air", "state": {"value": state}},
    }


@pytest.mark.parametrize("event", [census(0), off_air("ALARM")])
def test_an_always_on_channel_found_stopped_starts_a_run_about_it(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
) -> None:
    tables, _ = stage
    monkeypatch.setattr(aws, "client", lambda service: MediaTailor({"fast-demo": "STOPPED"}))

    found = runner.request_of(settings(tables), event)

    assert isinstance(found, runner.RunRequest)
    assert found.channel == "fast-demo" and found.correlation_id == event["id"]
    assert "STOPPED: fast-demo" in found.prompt and "channels___start_channel" in found.prompt
    assert found.facts["states"] == {"fast-demo": "STOPPED"}


@pytest.mark.parametrize(
    ("role", "event", "states", "reason"),
    [
        (Role.OPERATIONS, census(0), {"fast-demo": "RUNNING"}, "no always-on channel is stopped"),
        (Role.OPERATIONS, census(2, previous=0), {"fast-demo": "STOPPED"}, "not an off-air signal"),
        (Role.OPERATIONS, off_air("OK"), {"fast-demo": "STOPPED"}, "not an off-air signal"),
        (Role.PROGRAMMING, census(0), {"fast-demo": "STOPPED"}, "has no census trigger"),
    ],
)
def test_a_trigger_with_nothing_to_do_starts_no_run(
    stage: tuple[AgentTables, Topic],
    monkeypatch: pytest.MonkeyPatch,
    role: Role,
    event: dict[str, Any],
    states: dict[str, str],
    reason: str,
) -> None:
    tables, _ = stage
    monkeypatch.setattr(aws, "client", lambda service: MediaTailor(states))
    monkeypatch.setattr(runner, "runner_settings", lambda: settings(tables, role))
    scripted(monkeypatch, [])

    found = runner.handler(event, Context())

    assert reason in found["skipped"]
    assert tables.ledger_entries() == []


def test_without_an_always_on_channel_a_census_trigger_starts_no_run(stage: tuple[AgentTables, Topic]) -> None:
    tables, _ = stage
    cfg = dataclasses.replace(settings(tables), envelope=Envelope().resolved("fast"))

    assert runner.request_of(cfg, census(0)) == "no channel is always-on"
