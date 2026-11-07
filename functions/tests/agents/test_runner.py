"""The agent runner: approvals answered from the envelope and a read of its caps, every call ledgered, refusals
(and a propose-only agent's proposals) escalated, triggers turned into runs only when there is something to do."""

import dataclasses
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.agents.envelope import Cap, Envelope, Refusal, Role, window_of
from fast_channels.core.pds.book import Line
from fast_channels.core.pds.pacing import pacing
from fast_channels.functions import aws
from fast_channels.functions.agents import envelope_record, runner, sales
from fast_channels.functions.agents.caps import take
from fast_channels.functions.autopilot import AutopilotSettings, RunOutcome
from fast_channels.functions.settings import settings as function_settings
from fast_channels.functions.tools.registry import ToolError

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
        library_table=tables.library,
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
    assert tables.counts() == {}, "the runner only reads the caps: the Gateway's interceptor takes them"


def gateway_took(tables: AgentTables, *keys: str, limit: int = 1) -> None:
    """The caps a call let through by the Gateway's interceptor took (``take_caps=True``)."""
    window = window_of(datetime.now(UTC))
    for key in keys:
        assert take(tables.client, tables.agents, [Cap(key, limit, window)]) is None


def test_a_second_restart_within_the_hour_is_refused_and_escalated(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    for correlation in ("census-1", "census-2"):
        if correlation == "census-2":  # the first restart reached the Gateway, which took its caps
            gateway_took(tables, f"CAP#operations#{START}#fast-demo")
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


def test_the_runner_reads_the_caps_and_never_takes_them(stage: tuple[AgentTables, Topic]) -> None:
    tables, _ = stage
    cfg, now = settings(tables), datetime.now(UTC)
    ask = {"tool": START, "arguments": {"channel": "fast-demo"}}

    first, again = runner.decide(cfg, ask, now=now), runner.decide(cfg, ask, now=now)
    gateway_took(tables, f"CAP#operations#{START}#fast-demo")
    after = runner.decide(cfg, ask, now=now)

    assert first.approved and again.approved, "concurrent approvals race to the Gateway, whose take is atomic"
    assert not after.approved and after.refusal is Refusal.CAP
    assert sum(tables.counts().values()) == 1


def test_a_call_the_interceptor_refused_is_not_escalated_again(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    refused = {"ok": False, "error": {"code": "ENVELOPE", "message": "Outside your autonomy envelope: cap"}}
    scripted(
        monkeypatch,
        [
            events(
                *started("c1", START, {"channel": "fast-demo"}),
                approval("c1", START, {"channel": "fast-demo"}),
                FINISHED,
            ),
            events(result("c1", refused), said("The Gateway refused it."), FINISHED),
        ],
    )

    found = handle(tables, monkeypatch, request())

    assert found["calls"] == [(START, "envelope")] and found["intercepted"] == [START]
    assert not found["escalated"] and not topic.published, "the interceptor recorded and escalated it"


PROGRAMMED = Envelope(
    programmed=("{prefix}-demo",),
    agents={
        **Envelope().agents,
        Role.PROGRAMMING: Envelope().agents[Role.PROGRAMMING].model_copy(update={"enabled": True}),
    },
).resolved("fast")
PUBLISH = "schedule___publish"


@pytest.mark.parametrize(("ahead", "approved"), [(timedelta(minutes=30), False), (timedelta(hours=3), True)])
def test_a_publish_is_approved_only_when_its_plan_starts_two_hours_ahead(
    stage: tuple[AgentTables, Topic], ahead: timedelta, approved: bool
) -> None:
    tables, _ = stage
    cfg = dataclasses.replace(settings(tables, Role.PROGRAMMING), envelope=PROGRAMMED)
    now = datetime.now(UTC)
    tables.store_plan(
        "fast-demo.0a1b", start_ms=int((now + ahead).timestamp() * 1000), expires_at=int(time.time()) + 3600
    )

    decision = runner.decide(cfg, {"tool": PUBLISH, "arguments": {"plan_id": "fast-demo.0a1b"}}, now=now)
    missing = runner.decide(cfg, {"tool": PUBLISH, "arguments": {"plan_id": "fast-demo.ffff"}}, now=now)

    assert decision.approved is approved
    if not approved:
        assert decision.refusal is Refusal.LEAD and "120 minutes" in decision.reason
    assert not missing.approved and missing.refusal is Refusal.LEAD, "a plan with no known start is refused"


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


TICK: dict[str, Any] = {
    "id": "tick-1",
    "detail-type": "Scheduled Event",
    "source": "aws.events",
    "time": "2026-11-07T10:00:00Z",
    "detail": {},
}


@dataclass
class Schedules:
    """``GetChannelSchedule`` of Channel Assembly: one program ending ``ends`` ahead per channel (None: nothing)."""

    ends: dict[str, timedelta | None]

    def get_paginator(self, name: str) -> Schedules:
        assert name == "get_channel_schedule"
        return self

    def paginate(self, *, ChannelName: str, DurationMinutes: str) -> list[dict[str, Any]]:  # noqa: N803
        assert DurationMinutes == "1440"
        ahead = self.ends[ChannelName]
        if ahead is None:
            return [{"Items": []}]
        start = datetime.now(UTC) + ahead - timedelta(minutes=30)
        program = {"ScheduleEntryType": "PROGRAM", "ApproximateStartTime": start, "ApproximateDurationSeconds": 1800}
        slate = {**program, "ScheduleEntryType": "FILLER_SLATE", "ApproximateStartTime": start + timedelta(hours=9)}
        return [{"Items": [program, slate]}]


@pytest.mark.parametrize("ahead", [None, timedelta(hours=1)])
def test_a_short_runway_starts_a_programming_run_two_hours_ahead(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch, ahead: timedelta | None
) -> None:
    tables, _ = stage
    monkeypatch.setattr(aws, "client", lambda service: Schedules({"fast-demo": ahead}))
    cfg = dataclasses.replace(settings(tables, Role.PROGRAMMING), envelope=PROGRAMMED)

    found = runner.request_of(cfg, TICK)

    assert isinstance(found, runner.RunRequest)
    assert found.channel == "fast-demo" and found.correlation_id == "tick-1"
    assert "runbook R6" in found.prompt and "(pass it as start)" in found.prompt
    assert "at least 120 minutes from now" in found.prompt
    runway = found.facts["channels"]["fast-demo"]  # type: ignore[index, call-overload]
    assert runway["runway_minutes"] < 6 * 60  # type: ignore[index, call-overload]


def test_a_long_runway_or_no_programmed_channel_starts_no_run(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, _ = stage
    monkeypatch.setattr(aws, "client", lambda service: Schedules({"fast-demo": timedelta(hours=7)}))
    cfg = dataclasses.replace(settings(tables, Role.PROGRAMMING), envelope=PROGRAMMED)
    nothing = dataclasses.replace(cfg, envelope=Envelope().resolved("fast"))

    assert "reaches 6:00:00 ahead" in str(runner.request_of(cfg, TICK))
    assert runner.request_of(nothing, TICK) == "no channel is programmed"
    assert runner.request_of(settings(tables), TICK) == "the operations agent has no tick trigger"


def line(spent: str) -> tuple[Line, Decimal]:
    now = datetime.now(UTC)
    found = Line(
        id="direct-1",
        advertiser="Direct Advertiser 1",
        adomain="direct1.example",
        creatives=("direct1",),
        cpm=Decimal(8),
        budget=Decimal(100),
        starts=now - timedelta(days=1),
        ends=now + timedelta(days=9),
    )
    return found, Decimal(100) - Decimal(spent)


@pytest.mark.parametrize(("spent", "runs"), [("60", True), ("10", False)])
def test_an_off_pace_line_starts_a_sales_run(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch, spent: str, runs: bool
) -> None:
    tables, _ = stage
    found_line, left = line(spent)
    monkeypatch.setattr(sales, "pacings", lambda at: ("v1", [pacing(found_line, left, at)]))

    found = runner.request_of(settings(tables, Role.SALES), TICK)

    if runs:
        assert isinstance(found, runner.RunRequest) and found.channel is None
        assert "direct-1 (Direct Advertiser 1): overpacing, pace 6.00" in found.prompt
        assert "sales___propose" in found.prompt and found.facts["snapshot"] == "v1"
    else:
        assert "no own-book line is off pace" in str(found)


def test_an_unpublished_book_starts_no_sales_run(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, _ = stage

    def unpublished(at: datetime) -> tuple[str, list[Any]]:
        raise ToolError("NOT_FOUND", "no PDS snapshot is published yet")

    monkeypatch.setattr(sales, "pacings", unpublished)

    assert "could not be read" in str(runner.request_of(settings(tables, Role.SALES), TICK))


SALES_ON = Envelope(
    agents={**Envelope().agents, Role.SALES: Envelope().agents[Role.SALES].model_copy(update={"enabled": True})}
).resolved("fast")


def test_a_propose_only_agents_proposal_is_escalated_to_a_human(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    tables, topic = stage
    monkeypatch.setattr(
        runner, "runner_settings", lambda: dataclasses.replace(settings(tables, Role.SALES), envelope=SALES_ON)
    )
    proposal = {"proposal_id": "01J", "line": "direct-1", "action": "change_budget", "value": "60"}
    scripted(
        monkeypatch,
        [
            events(
                *started("c0", "sales___line_pacing", {}),
                result("c0", {"lines": [{"line": "direct-1", "state": "overpacing"}]}),
                *started("c1", "sales___propose", {"line": "direct-1", "action": "change_budget"}),
                result("c1", proposal),
                said("Proposed a lower budget for direct-1."),
                FINISHED,
            )
        ],
    )
    sales_request = runner.RunRequest(prompt="direct-1 is overpacing.", correlation_id="tick-1")

    found = runner.handler(sales_request.model_dump(mode="json"), Context())

    assert found["escalated"] and found["proposals"] == 1 and not found["refused"]
    [message] = topic.published
    assert "only proposes" in message["Message"] and "change_budget" in message["Message"]
    [escalation] = [detail for action, _, detail in ledger(tables) if action == "agent.escalate"]
    assert escalation["proposals"] and escalation["refused"] == []


def test_a_programming_run_that_leaves_the_runway_short_is_escalated(
    stage: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent finishes without publishing (it found nothing it would air): the runner reads the runway again and
    a human hears of it, though nothing was refused."""
    tables, topic = stage
    schedules = Schedules({"fast-demo": None})
    monkeypatch.setattr(aws, "client", lambda service: topic if service == "sns" else schedules)
    monkeypatch.setattr(
        runner, "runner_settings", lambda: dataclasses.replace(settings(tables, Role.PROGRAMMING), envelope=PROGRAMMED)
    )
    scripted(
        monkeypatch,
        [
            events(
                *started("c0", "library___find_assets", {"ladder_version": "standard"}),
                result("c0", {"assets": []}),
                said("No READY asset I would air; nothing published."),
                FINISHED,
            )
        ],
    )

    found = runner.handler(TICK, Context())

    assert found["escalated"] and not found["refused"] and not found["error"]
    assert found["unresolved"] == ["fast-demo: its schedule still ends 0 minutes ahead (runbook R6 needs 360)"]
    [message] = topic.published
    assert "left a problem" in message["Message"] and "still ends 0 minutes ahead" in message["Message"]
    [escalation] = [detail for action, _, detail in ledger(tables) if action == "agent.escalate"]
    assert escalation["unresolved"] and escalation["facts"]["trigger"] == "tick"
