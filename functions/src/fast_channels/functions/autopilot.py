"""Autopilot: keeps a channel's schedule filled to a horizon by asking the scheduling agent, unattended.

EventBridge Scheduler invokes this Lambda once per ``autopilot_cron`` tick of a channel with ``{"channel": ...}``. It
authenticates as the autopilot Cognito app client (client credentials; the secret is read with
DescribeUserPoolClient), runs one AG-UI conversation with the AgentCore Runtime (CONTRACTS §G) and publishes an
``AutopilotRun`` event with the agent's closing summary.

The agent pauses before every CONFIRM tool and waits for approval. Nobody watches an autopilot run, so this function
answers those interrupts itself: it approves ``schedule___publish`` (the only CONFIRM tool the Gateway's Cedar policy
allows the autopilot client, SPEC §6) and declines everything else, so an unattended run never starts channels,
creates resources or changes ladders.
"""

import json
import os
import time
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Self

import httpx
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import BaseModel, Field

from fast_channels.core.events import AutopilotRun, put_events_entry
from fast_channels.functions.aws import client
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

APPROVED_TOOL: Final = "schedule___publish"
APPROVAL_EVENT: Final = "approval_required"
SESSION_ID_MIN: Final = 33  # AgentCore Runtime session ids are 33-256 characters (agentcore.md §5)
MAX_RUNS: Final = 30
"""Upper bound on AG-UI runs per invocation: the first prompt plus one resume per publish approval."""
SAFETY_MARGIN_MS: Final = 90_000
"""Stop resuming when less than this is left of the Lambda timeout (a run can take a minute or more)."""
TOKEN_EXPIRY_MARGIN_S: Final = 60
SUMMARY_MAX: Final = 2_000
DECLINE_NOTE: Final = (
    "Autopilot runs unattended and may only publish schedules. Leave this change for an operator and continue."
)
_TIMEOUT: Final = httpx.Timeout(connect=10, read=300, write=30, pool=10)  # SSE events can be minutes apart


@dataclass(frozen=True, slots=True)
class AutopilotSettings:
    """Environment of the autopilot function (set by the ``Autopilot`` construct)."""

    runtime_invoke_url: str
    """``https://bedrock-agentcore.<r>.amazonaws.com/runtimes/<url-encoded arn>/invocations?qualifier=DEFAULT``."""
    user_pool_id: str
    client_id: str
    token_endpoint: str
    """``https://<domain>.auth.<r>.amazoncognito.com/oauth2/token``."""
    scope: str
    """OAuth scope the Runtime's JWT authorizer expects, e.g. ``fast-channels/agent.invoke``."""

    @classmethod
    def from_env(cls) -> Self:
        names = ("RUNTIME_INVOKE_URL", "USER_POOL_ID", "AUTOPILOT_CLIENT_ID", "TOKEN_ENDPOINT", "SCOPE")
        if missing := [name for name in names if not os.environ.get(name)]:
            raise RuntimeError(f"missing environment configuration: {', '.join(missing)}")
        url, pool, client_id, endpoint, scope = (os.environ[name] for name in names)
        return cls(runtime_invoke_url=url, user_pool_id=pool, client_id=client_id, token_endpoint=endpoint, scope=scope)


@cache
def autopilot_settings() -> AutopilotSettings:
    return AutopilotSettings.from_env()


@cache
def http() -> httpx.Client:
    """One pooled HTTPS client per execution environment."""
    return httpx.Client(timeout=_TIMEOUT)


class AutopilotRequest(BaseModel):
    """Input the EventBridge Scheduler schedule sends."""

    channel: str = Field(min_length=1, max_length=64)
    horizon_hours: int = Field(24, ge=1, le=168)


# --- Cognito client-credentials token ---------------------------------------------------------------------------------


@cache
def _client_secret(user_pool_id: str, client_id: str) -> str:
    response = client("cognito-idp").describe_user_pool_client(UserPoolId=user_pool_id, ClientId=client_id)
    return str(response["UserPoolClient"]["ClientSecret"])


@dataclass(slots=True)
class TokenCache:
    """The autopilot's access token, reused across invocations until shortly before it expires."""

    value: str = ""
    valid_until: float = 0.0

    def get(self, cfg: AutopilotSettings) -> str:
        if self.value and time.monotonic() < self.valid_until:
            return self.value
        response = http().post(
            cfg.token_endpoint,
            data={"grant_type": "client_credentials", "scope": cfg.scope},
            auth=(cfg.client_id, _client_secret(cfg.user_pool_id, cfg.client_id)),
        )
        response.raise_for_status()
        body = response.json()
        self.value = str(body["access_token"])
        self.valid_until = time.monotonic() + int(body["expires_in"]) - TOKEN_EXPIRY_MARGIN_S
        return self.value


_tokens = TokenCache()


# --- AG-UI over the AgentCore Runtime ---------------------------------------------------------------------------------


def session_id_for(channel: str, now: datetime) -> str:
    """``autopilot-<channel>-<UTC hour>``, zero-padded to the Runtime minimum.

    One session per channel and hour: a retried invocation continues the same conversation instead of starting over.
    """
    return f"autopilot-{channel}-{now.astimezone(UTC):%Y%m%dT%H}".ljust(SESSION_ID_MIN, "0")


def prompt_for(request: AutopilotRequest) -> str:
    return (
        f"Autopilot run for channel {request.channel}. Nobody is watching this conversation, so do not ask "
        "questions. Extend the published schedule so that it reaches at least "
        f"{request.horizon_hours} hours from now, following the channel's programming brief: read the brief and the "
        "current schedule, plan from the end of the published schedule in blocks, and publish every plan that has "
        "no error violations. Finish with a short summary: blocks published, the time range covered and any problem "
        "that needs an operator."
    )


@dataclass(slots=True)
class RunOutcome:
    """What one AG-UI run produced."""

    text: dict[str, str] = field(default_factory=dict)
    """Assistant text by message id, in arrival order."""
    tools: list[str] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    """``approval_required`` values the run paused on."""
    error: str | None = None
    finished: bool = False

    @property
    def summary(self) -> str:
        """The last assistant message of the run."""
        return next(reversed(self.text.values()), "").strip()

    def apply(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "TEXT_MESSAGE_CONTENT":
                key = str(event.get("messageId", ""))
                self.text[key] = self.text.get(key, "") + str(event.get("delta", ""))
            case "TOOL_CALL_START":
                self.tools.append(str(event.get("toolCallName", "")))
            case "CUSTOM" if event.get("name") == APPROVAL_EVENT:
                self.approvals.append(dict(event.get("value") or {}))
            case "RUN_ERROR":
                self.error = f"{event.get('code', 'RUN_ERROR')}: {event.get('message', '')}"
            case "RUN_FINISHED":
                self.finished = True


def sse_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """JSON payloads of a ``text/event-stream`` (``data:`` lines joined per event; unterminated events dropped)."""
    data: list[str] = []
    for line in lines:
        if not line:
            if data:
                yield json.loads("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").removeprefix(" "))


def answers(approvals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """AG-UI ``resume`` entries: publish is approved, anything else declined with a note the model reads."""
    entries = []
    for approval in approvals:
        approved = approval.get("tool") == APPROVED_TOOL
        payload = {"approved": True} if approved else {"approved": False, "note": DECLINE_NOTE}
        entries.append({"interruptId": approval["interrupt_id"], "status": "resolved", "payload": payload})
    return entries


def run_once(
    cfg: AutopilotSettings, *, session_id: str, channel: str, prompt: str | None, resume: list[dict[str, Any]]
) -> RunOutcome:
    """POST one ``RunAgentInput`` and drain its SSE stream.

    Platform failures (401/403) also arrive as an SSE ``RUN_ERROR`` body, so the body is parsed on every status.
    """
    body = {
        "threadId": session_id,
        "runId": uuid.uuid4().hex,
        "messages": [{"id": uuid.uuid4().hex, "role": "user", "content": prompt}] if prompt else [],
        "state": {"channel": channel},
        "tools": [],
        "context": [],
        "forwardedProps": {"mode": "plan"},
        "resume": resume,
    }
    headers = {
        "Authorization": f"Bearer {_tokens.get(cfg)}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        "Accept": "text/event-stream",
    }
    if trace := os.environ.get("_X_AMZN_TRACE_ID"):
        headers["X-Amzn-Trace-Id"] = trace  # joins the Runtime's spans to this invocation's trace
    outcome = RunOutcome()
    with http().stream("POST", cfg.runtime_invoke_url, json=body, headers=headers) as response:
        for event in sse_events(response.iter_lines()):
            outcome.apply(event)
        if response.status_code != httpx.codes.OK and outcome.error is None:
            outcome.error = f"HTTP {response.status_code}"
    return outcome


@dataclass(slots=True)
class Conversation:
    """The runs of one autopilot invocation."""

    runs: int = 0
    publishes_approved: int = 0
    tools: list[str] = field(default_factory=list)
    summary: str = ""
    error: str | None = None


def converse(
    cfg: AutopilotSettings, request: AutopilotRequest, *, session_id: str, context: LambdaContext
) -> Conversation:
    """Prompt the agent, then answer its approval interrupts until it finishes, fails or time runs out."""
    result = Conversation()
    prompt: str | None = prompt_for(request)
    resume: list[dict[str, Any]] = []
    while True:
        outcome = run_once(cfg, session_id=session_id, channel=request.channel, prompt=prompt, resume=resume)
        result.runs += 1
        result.tools += outcome.tools
        result.summary = outcome.summary or result.summary
        if outcome.error or not outcome.finished:
            result.error = outcome.error or "the run ended without RUN_FINISHED"
            return result
        if not outcome.approvals:
            return result
        if result.runs >= MAX_RUNS or context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS:
            result.error = f"stopped after {result.runs} runs with {len(outcome.approvals)} approvals pending"
            return result
        resume = answers(outcome.approvals)
        result.publishes_approved += sum(entry["payload"]["approved"] for entry in resume)
        prompt = None


def _emit(detail: AutopilotRun) -> None:
    cfg = settings()
    entry = put_events_entry(detail, prefix=cfg.prefix, event_bus=cfg.event_bus)
    response = client("events").put_events(Entries=[entry])
    if response.get("FailedEntryCount"):
        logger.error("AutopilotRun event rejected", extra={"entries": response.get("Entries")})


class AutopilotError(RuntimeError):
    """Raised after the FAILED event is published, so the asynchronous invocation is retried or dead-lettered."""


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """EventBridge Scheduler target: one unattended scheduling conversation for ``event["channel"]``."""
    request = AutopilotRequest.model_validate(event)
    session_id = session_id_for(request.channel, datetime.now(UTC))
    logger.set_correlation_id(session_id)
    logger.append_keys(channel=request.channel)

    result = converse(autopilot_settings(), request, session_id=session_id, context=context)
    status = "FAILED" if result.error else "SUCCEEDED"
    summary = (result.error + "\n" if result.error else "") + result.summary
    detail = AutopilotRun(channel=request.channel, status=status, session_id=session_id, summary=summary[:SUMMARY_MAX])
    _emit(detail)
    metrics.add_dimension(name="channel", value=request.channel)
    metrics.add_metric(name=f"Autopilot{status.title()}", unit=MetricUnit.Count, value=1)
    metrics.add_metric(name="AutopilotPublishes", unit=MetricUnit.Count, value=result.publishes_approved)
    logger.info(
        "autopilot run finished",
        extra={
            "status": status,
            "runs": result.runs,
            "publishes_approved": result.publishes_approved,
            "tools": result.tools,
            "summary": detail.summary,
        },
    )
    if result.error:
        raise AutopilotError(result.error)
    return detail.model_dump(mode="json")
