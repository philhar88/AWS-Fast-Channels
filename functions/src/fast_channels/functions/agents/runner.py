"""The agent runner (M9): one unattended conversation of a roster agent, inside the autonomy envelope.

A trigger invokes the runner of one agent (``ROLE``) with what happened. The runner authenticates as the agent's
Cognito machine client, so the scheduler agent's Runtime and the Gateway see the agent's own principal, and holds an
AG-UI conversation with the Runtime as the autopilot does (``functions.autopilot``, whose transport it reuses). The
Gateway's Cedar policies, generated from the envelope, deny every tool call outside the agent's grants. The runner is
the second enforcement point: the agent pauses before every approval-gated call, and the runner answers it with
``core.agents.envelope.verdict`` and the caps' counter (``functions.agents.caps``), never with the LLM.

Every approval decision and every tool call goes to the ledger, in the agent's chain ``agents/<role>`` under the
agent's principal, with the envelope's version and the trigger's correlation id. When anything was refused (by the
runner, or by Cedar), the runner escalates to a human: one message on the stage's alert topic and an
``agent.escalate`` entry, with the agent's own summary of the run.
"""

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Self

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import BaseModel, Field

from fast_channels.core.agents.envelope import Envelope, Refusal, Role, verdict
from fast_channels.core.agents.names import ESCALATIONS_METRIC
from fast_channels.core.ledger import Json, LedgerKind
from fast_channels.functions import aws
from fast_channels.functions.agents import operations
from fast_channels.functions.agents.caps import take
from fast_channels.functions.autopilot import (
    APPROVAL_EVENT,
    SESSION_ID_MIN,
    AutopilotSettings,
    RunOutcome,
    ToolCall,
    run_once,
)
from fast_channels.functions.ops.ledger import append, entry

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

MAX_RUNS: Final = 12
"""AG-UI runs per invocation: the first prompt plus a resume per round of approvals."""
SAFETY_MARGIN_MS: Final = 60_000
REPORT_MARGIN_MS: Final = 30_000
SUMMARY_MAX: Final = 2_000
DENIED_WORDS: Final = ("not authorized", "unauthorized", "access denied", "accessdenied", "forbidden", "denied by")
"""A failed tool call whose message says one of these was denied before it reached the tool (Cedar)."""
UNATTENDED: Final = (
    "Nobody is watching this conversation, so do not ask questions. Use only the tools you need; approval-gated "
    "calls are decided by your autonomy envelope, and a declined call stays declined: do not retry it, say what a "
    "human must do instead. Finish with a short summary of what you did and of anything that needs a human."
)


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    """Environment of a runner function (set by the ``AgentRoster`` construct)."""

    role: Role
    transport: AutopilotSettings
    """The Runtime's URL and the agent's Cognito client (the autopilot's transport settings)."""
    envelope: Envelope
    """The envelope, resolved for the deployment."""
    table: str
    """The agents' table (the caps' counters)."""
    ledger_table: str
    alert_topic: str
    prefix: str

    @classmethod
    def from_env(cls) -> Self:
        names = (
            "ROLE",
            "RUNTIME_INVOKE_URL",
            "USER_POOL_ID",
            "AGENT_CLIENT_ID",
            "TOKEN_ENDPOINT",
            "SCOPE",
            "ENVELOPE",
            "AGENTS_TABLE",
            "LEDGER_TABLE",
            "ALERT_TOPIC_ARN",
            "FAST_PREFIX",
        )
        if missing := [name for name in names if not os.environ.get(name)]:
            raise RuntimeError(f"missing environment configuration: {', '.join(missing)}")
        env = {name: os.environ[name] for name in names}
        return cls(
            role=Role(env["ROLE"]),
            transport=AutopilotSettings(
                runtime_invoke_url=env["RUNTIME_INVOKE_URL"],
                user_pool_id=env["USER_POOL_ID"],
                client_id=env["AGENT_CLIENT_ID"],
                token_endpoint=env["TOKEN_ENDPOINT"],
                scope=env["SCOPE"],
            ),
            envelope=Envelope.model_validate_json(env["ENVELOPE"]),
            table=env["AGENTS_TABLE"],
            ledger_table=env["LEDGER_TABLE"],
            alert_topic=env["ALERT_TOPIC_ARN"],
            prefix=env["FAST_PREFIX"],
        )


@cache
def runner_settings() -> RunnerSettings:
    return RunnerSettings.from_env()


class RunRequest(BaseModel):
    """What a trigger asks an agent to look at."""

    prompt: str = Field(min_length=1, max_length=8_000, description="What happened, and what the agent should do.")
    channel: str | None = Field(None, max_length=64, description="The channel it concerns, if one.")
    correlation_id: str = Field(min_length=1, max_length=256, description="The trigger's id (an alarm, an event).")
    facts: dict[str, Json] = Field(default_factory=dict, description="What the trigger knew, for the ledger.")


def session_id_for(role: Role, correlation_id: str) -> str:
    """``agent-<role>-<digest of the trigger>``: a retried invocation continues the same conversation."""
    digest = hashlib.sha256(correlation_id.encode()).hexdigest()[:24]
    return f"agent-{role.value}-{digest}".ljust(SESSION_ID_MIN, "0")


@dataclass(slots=True)
class Decision:
    """The runner's answer to one approval-gated call."""

    tool: str
    arguments: dict[str, Any]
    approved: bool
    reason: str
    refusal: Refusal | None = None
    channel: str | None = None


@dataclass(slots=True)
class Ledgered:
    """What one invocation recorded."""

    decisions: list[Decision] = field(default_factory=list)
    calls: list[tuple[str, ToolCall, str]] = field(default_factory=list)
    """Each tool call with its outcome (``done``, ``error``, ``declined``, ``refused``)."""
    pending: dict[str, ToolCall] = field(default_factory=dict)
    """Calls that paused for approval, by id, until their result arrives."""
    runs: int = 0
    summary: str = ""
    error: str | None = None

    @property
    def refused(self) -> list[str]:
        """Why each refused action was refused: the runner's refusals and Cedar's denials."""
        reasons = [f"{d.tool}: {d.reason}" for d in self.decisions if not d.approved]
        return reasons + [
            f"{call.name}: denied by the Gateway" for _, call, outcome in self.calls if outcome == "refused"
        ]


def decide(cfg: RunnerSettings, approval: Mapping[str, Any], *, now: datetime) -> Decision:
    """Answer one ``approval_required`` interrupt from the envelope, taking the action's caps when it is inside."""
    tool = str(approval.get("tool", ""))
    arguments = dict(approval.get("arguments") or {})
    found = verdict(cfg.envelope, cfg.role, tool, arguments, now=now)
    if not found.allowed:
        return Decision(tool, arguments, False, found.reason, found.refusal, found.channel)
    full = take(aws.client("dynamodb"), cfg.table, found.caps)
    if full is not None:
        reason = f"{tool}: the cap of {full.limit} an hour ({full.key}) is reached"
        return Decision(tool, arguments, False, reason, Refusal.CAP, found.channel)
    return Decision(tool, arguments, True, found.reason, None, found.channel)


def resume_entries(approvals: Iterable[Mapping[str, Any]], decisions: Iterable[Decision]) -> list[dict[str, Any]]:
    """AG-UI ``resume`` entries for the decisions, in the approvals' order."""
    return [
        {
            "interruptId": approval["interrupt_id"],
            "status": "resolved",
            "payload": {"approved": True}
            if decision.approved
            else {"approved": False, "note": f"Outside your autonomy envelope: {decision.reason}. A human decides."},
        }
        for approval, decision in zip(approvals, decisions, strict=True)
    ]


def outcome_of(call: ToolCall) -> str:
    """``done``, ``declined`` (the runner said no), ``refused`` (Cedar denied it) or ``error``."""
    try:
        payload = json.loads(call.result or "")
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or payload.get("ok") is not False:
        return "done" if call.result is not None else "error"
    error = payload.get("error") or {}
    if error.get("code") == "DECLINED":
        return "declined"
    message = str(error.get("message", "")).lower()
    return "refused" if any(word in message for word in DENIED_WORDS) else "error"


def _record(cfg: RunnerSettings, action: str, subject: str, request: RunRequest, detail: dict[str, Json]) -> None:
    append(
        entry(
            cfg.role.chain_id,
            LedgerKind.AGENT,
            principal=cfg.role.principal,
            action=action,
            subject=subject[:256],
            correlation_id=request.correlation_id,
            detail={"envelope": cfg.envelope.version, **detail},
        ),
        table=cfg.ledger_table,
    )


def record_decision(cfg: RunnerSettings, request: RunRequest, decision: Decision) -> None:
    _record(
        cfg,
        "agent.approve" if decision.approved else "agent.refuse",
        decision.tool,
        request,
        {
            "tool": decision.tool,
            "channel": decision.channel,
            "reason": decision.reason,
            "refusal": decision.refusal.value if decision.refusal else None,
            "input_sha256": _digest(json.dumps(decision.arguments, sort_keys=True)),
        },
    )


def record_call(cfg: RunnerSettings, request: RunRequest, call_id: str, call: ToolCall, outcome: str) -> None:
    _record(
        cfg,
        "agent.tool",
        call.name,
        request,
        {
            "tool": call.name,
            "call_id": call_id,
            "outcome": outcome,
            "input_sha256": _digest(call.arguments),
            "result": (call.result or "")[:500],
        },
    )


def converse(cfg: RunnerSettings, request: RunRequest, *, session_id: str, context: LambdaContext) -> Ledgered:
    """Prompt the agent, answer its approvals from the envelope, record everything; never raises."""
    done = Ledgered()
    prompt: str | None = f"{request.prompt}\n\n{UNATTENDED}"
    resume: list[dict[str, Any]] = []
    while True:
        if (left_ms := context.get_remaining_time_in_millis()) < SAFETY_MARGIN_MS:
            done.error = f"stopped after {done.runs} runs with {left_ms / 1000:.0f} s of the invocation left"
            return done
        try:
            outcome = run_once(
                cfg.transport,
                session_id=session_id,
                channel=request.channel or "",
                prompt=prompt,
                resume=resume,
                budget_s=(left_ms - REPORT_MARGIN_MS) / 1000,
            )
        except Exception as error:  # every failure still ends as a recorded, escalated run
            logger.exception("agent run failed")
            outcome = RunOutcome(error=f"{type(error).__name__}: {error}")
        done.runs += 1
        _record_calls(cfg, request, outcome, done)
        done.summary = outcome.summary or done.summary
        if outcome.error or not outcome.finished:
            done.error = outcome.error or "the run ended without RUN_FINISHED"
            return done
        if not outcome.approvals:
            return done
        if done.runs >= MAX_RUNS:
            done.error = f"stopped after {done.runs} runs with {len(outcome.approvals)} approvals pending"
            return done
        now = datetime.now(UTC)
        decisions = [decide(cfg, approval, now=now) for approval in outcome.approvals]
        for decision in decisions:
            record_decision(cfg, request, decision)
        done.decisions += decisions
        resume = resume_entries(outcome.approvals, decisions)
        prompt = None


def _record_calls(cfg: RunnerSettings, request: RunRequest, outcome: RunOutcome, done: Ledgered) -> None:
    for call_id, found in outcome.calls.items():
        call = found
        if not call.name and (started := done.pending.pop(call_id, None)) is not None:
            call = ToolCall(started.name, started.arguments, found.result)  # resumed after its approval
        if call.result is None:
            done.pending[call_id] = call  # paused for approval: recorded with its result in the next run
            continue
        result = outcome_of(call)
        record_call(cfg, request, call_id, call, result)
        done.calls.append((call_id, call, result))


def escalate(cfg: RunnerSettings, request: RunRequest, done: Ledgered) -> None:
    """Tell a human what was refused (or what failed), on the alert topic and in the ledger."""
    refused = done.refused
    problem = "; ".join(refused) or done.error or "the run failed"
    subject = f"[{cfg.prefix}] the {cfg.role.value} agent needs a human"
    message = "\n".join(
        [
            f"The {cfg.role.value} agent ({cfg.role.principal}) was refused or failed while handling "
            f"{request.correlation_id}.",
            f"Outside its envelope ({cfg.envelope.version}): {problem}",
            *([f"Channel: {request.channel}"] if request.channel else []),
            "",
            f"The agent's summary: {done.summary or '(none)'}",
            "",
            f"Ledger chain: {cfg.role.chain_id}",
        ]
    )
    aws.client("sns").publish(TopicArn=cfg.alert_topic, Subject=subject[:100], Message=message)
    _record(
        cfg,
        "agent.escalate",
        request.channel or request.correlation_id,
        request,
        {"refused": list(refused), "error": done.error, "summary": done.summary[:SUMMARY_MAX], "facts": request.facts},
    )
    metrics.add_metric(name=ESCALATIONS_METRIC, unit=MetricUnit.Count, value=1)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def request_of(cfg: RunnerSettings, event: Mapping[str, Any]) -> RunRequest | str:
    """The run a trigger asks for, or why there is none.

    An operations trigger (``functions.agents.operations``) becomes a run only when an always-on channel is
    stopped, read here before any model call; any other event is a :class:`RunRequest` itself (a direct
    invocation)."""
    found = operations.trigger_of(event)
    if found is None:
        return RunRequest.model_validate(event)
    kind, facts = found
    if cfg.role is not Role.OPERATIONS:
        return f"the {cfg.role.value} agent has no {kind} trigger"
    if (kind, facts.get("running" if kind == "census" else "state")) not in {("census", 0), ("alarm", "ALARM")}:
        return f"the {kind} trigger is not an off-air signal: {facts}"
    if not cfg.envelope.always_on:
        return "no channel is always-on"
    states = operations.channel_states(cfg.envelope.always_on)
    stopped = [name for name, state in states.items() if state == "STOPPED"]
    if not stopped:
        return f"no always-on channel is stopped: {states}"
    return RunRequest(
        prompt=operations.prompt(kind, facts, stopped),
        channel=stopped[0],
        correlation_id=str(event.get("id") or event.get("time") or kind),
        facts={"trigger": kind, **facts, "states": dict(states)},
    )


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One unattended conversation of the runner's agent about ``event``."""
    cfg = runner_settings()
    request = request_of(cfg, event)
    if isinstance(request, str):
        logger.info("nothing for the agent to do", extra={"role": cfg.role.value, "reason": request})
        return {"role": cfg.role.value, "skipped": request}
    session_id = session_id_for(cfg.role, request.correlation_id)
    logger.set_correlation_id(request.correlation_id)
    logger.append_keys(role=cfg.role.value, session_id=session_id)
    metrics.add_dimension(name="role", value=cfg.role.value)
    done = converse(cfg, request, session_id=session_id, context=context)
    escalated = bool(done.refused or done.error)
    if escalated:
        escalate(cfg, request, done)
    result = {
        "role": cfg.role.value,
        "session_id": session_id,
        "runs": done.runs,
        "approved": [d.tool for d in done.decisions if d.approved],
        "refused": done.refused,
        "calls": [(call.name, outcome) for _, call, outcome in done.calls],
        "escalated": escalated,
        "error": done.error,
        "summary": done.summary[:SUMMARY_MAX],
    }
    logger.info("agent run finished", extra=result)
    return result


__all__ = ["APPROVAL_EVENT", "handler"]
