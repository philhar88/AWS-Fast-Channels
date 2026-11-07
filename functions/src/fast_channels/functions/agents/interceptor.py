"""The Gateway's request interceptor (M9): the autonomy envelope, enforced on every tool call an agent makes.

The AgentCore Gateway passes no caller identity to its Lambda targets (``functions.tools.dispatch``), so the tools
cannot tell an agent from an operator. Its REQUEST interceptor can: the Gateway invokes it after validating the
caller's JWT and before calling the target, with the MCP request and, with ``passRequestHeaders``, its
``Authorization`` header (gateway-interceptors-types.html). For a ``tools/call`` whose token's ``client_id`` is a
roster agent's machine client, and whose tool is not READ, this function applies the envelope
(``functions.agents.enforce``): the verdict (scope, level, needs-human), the publish lead from the stored plan, and the
caps, which a call inside the envelope takes atomically. So an agent's limits hold however its token reaches the
Gateway, with or without the runner in front of it.

- A call inside the envelope is recorded (``agent.allow`` in the agent's chain, ``source: gateway``) and passed on
  unchanged.
- A call outside it never reaches the tool: the Gateway answers with the tools' error envelope, code ``ENVELOPE``. It
  is recorded (``agent.refuse``) and escalated to a human (the stage's alert topic and ``agent.escalate``).
- When the check itself fails (a read of the plan or the caps, the ledger), the agent's call is refused: an
  unchecked or unrecorded action is never let through.
- Every other request (operators' and the autopilot's tokens, ``tools/list``, READ tools) passes untouched, with no
  AWS call.

The interceptor runs before the Gateway's Cedar policies (seen on a deployed stage: an L1 agent's approval-gated
call, which Cedar forbids, reached it and was refused here), so it records every agent call it refuses.
"""

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Self

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.agents.envelope import Envelope, Refusal, Role, Verdict
from fast_channels.core.agents.names import ESCALATIONS_METRIC
from fast_channels.core.ledger import Json, LedgerKind
from fast_channels.functions import aws
from fast_channels.functions.agents.enforce import check
from fast_channels.functions.ops.ledger import append, entry
from fast_channels.functions.tools.registry import CORRELATION_FIELD

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

VERSION: Final = "1.0"
REFUSED_CODE: Final = "ENVELOPE"
"""The error code of a call the envelope refused (the runner tells it from Cedar's denials and tool failures)."""
CLIENT_CLAIM: Final = "client_id"


@dataclass(frozen=True, slots=True)
class InterceptorSettings:
    """Environment of the interceptor (set by the ``AgentRoster`` construct)."""

    envelope: Envelope
    """The envelope, resolved for the deployment."""
    clients: Mapping[str, Role]
    """Each enabled agent's Cognito client id."""
    tools: Mapping[str, bool]
    """Every tool that is not READ, and whether it is approval-gated (CONFIRM)."""
    agents_table: str
    library_table: str
    ledger_table: str
    alert_topic: str
    prefix: str

    @classmethod
    def from_env(cls) -> Self:
        names = ("ENVELOPE", "TOOLS", "AGENTS_TABLE", "LIBRARY_TABLE", "LEDGER_TABLE", "ALERT_TOPIC_ARN", "FAST_PREFIX")
        if missing := [name for name in names if not os.environ.get(name)]:
            raise RuntimeError(f"missing environment configuration: {', '.join(missing)}")
        clients = {os.environ[f"CLIENT_{role.name}"]: role for role in Role if os.environ.get(f"CLIENT_{role.name}")}
        return cls(
            envelope=Envelope.model_validate_json(os.environ["ENVELOPE"]),
            clients=clients,
            tools={str(name): bool(gated) for name, gated in json.loads(os.environ["TOOLS"]).items()},
            agents_table=os.environ["AGENTS_TABLE"],
            library_table=os.environ["LIBRARY_TABLE"],
            ledger_table=os.environ["LEDGER_TABLE"],
            alert_topic=os.environ["ALERT_TOPIC_ARN"],
            prefix=os.environ["FAST_PREFIX"],
        )


@cache
def interceptor_settings() -> InterceptorSettings:
    return InterceptorSettings.from_env()


def claims_of(headers: Mapping[str, Any]) -> dict[str, Any]:
    """The claims of the request's bearer token (already validated by the Gateway); empty when there is none."""
    value = next((str(v) for k, v in headers.items() if str(k).lower() == "authorization"), "")
    scheme, _, token = value.partition(" ")
    parts = token.split(".") if scheme.lower() == "bearer" else []
    if len(parts) != 3:
        return {}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except binascii.Error, ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def passed(body: Any) -> dict[str, Any]:
    """The interceptor's answer that lets the request through unchanged."""
    return {"interceptorOutputVersion": VERSION, "mcp": {"transformedGatewayRequest": {"body": body}}}


def refused(body: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """The interceptor's answer to a refused call: the tools' error envelope, as the Gateway returns a tool's."""
    error = {
        "ok": False,
        "error": {
            "code": REFUSED_CODE,
            "message": f"Outside your autonomy envelope: {reason}. A human decides.",
            "retryable": False,
            "hint": "Do not retry this call; say what a human must do instead.",
        },
    }
    result = {"content": [{"type": "text", "text": json.dumps(error)}], "isError": True}
    response = {"statusCode": 200, "body": {"jsonrpc": "2.0", "id": body.get("id"), "result": result}}
    return {"interceptorOutputVersion": VERSION, "mcp": {"transformedGatewayResponse": response}}


def _record(
    cfg: InterceptorSettings, role: Role, action: str, subject: str, *, correlation_id: str, detail: dict[str, Json]
) -> None:
    append(
        entry(
            role.chain_id,
            LedgerKind.AGENT,
            principal=role.principal,
            action=action,
            subject=subject[:256],
            correlation_id=correlation_id,
            detail={"envelope": cfg.envelope.version, "source": "gateway", **detail},
        ),
        table=cfg.ledger_table,
    )


def escalate(cfg: InterceptorSettings, role: Role, tool: str, found: Verdict, correlation_id: str) -> None:
    """Tell a human that the Gateway refused an agent's call: the alert topic, then ``agent.escalate``."""
    subject = f"[{cfg.prefix}] the {role.value} agent needs a human"
    message = "\n".join(
        [
            f"The Gateway refused a tool call of the {role.value} agent ({role.principal}): {found.reason}.",
            f"Envelope: {cfg.envelope.version}",
            *([f"Channel: {found.channel}"] if found.channel else []),
            f"Correlation id: {correlation_id}",
            "",
            f"Ledger chain: {role.chain_id}",
        ]
    )
    aws.client("sns").publish(TopicArn=cfg.alert_topic, Subject=subject[:100], Message=message)
    _record(
        cfg,
        role,
        "agent.escalate",
        found.channel or tool,
        correlation_id=correlation_id,
        detail={
            "refused": [found.reason if found.reason.startswith(tool) else f"{tool}: {found.reason}"],
            "error": None,
            "summary": "",
            "facts": {"tool": tool},
        },
    )
    metrics.add_metric(name=ESCALATIONS_METRIC, unit=MetricUnit.Count, value=1)


def _detail(tool: str, found: Verdict, digest: str) -> dict[str, Json]:
    return {
        "tool": tool,
        "channel": found.channel,
        "reason": found.reason,
        "refusal": found.refusal.value if found.refusal else None,
        "input_sha256": digest,
    }


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """One Gateway request: an agent's non-READ tool call is checked against the envelope; the rest passes."""
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    body = request.get("body")
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return passed(body)
    cfg = interceptor_settings()
    role = cfg.clients.get(str(claims_of(request.get("headers") or {}).get(CLIENT_CLAIM, "")))
    params = body.get("params") or {}
    tool = str(params.get("name", ""))
    if role is None or tool not in cfg.tools:
        return passed(body)  # an operator, the autopilot, or a READ tool (Cedar decides those)
    arguments = params.get("arguments") or {}
    correlation_id = str(arguments.get(CORRELATION_FIELD) or context.aws_request_id)
    logger.append_keys(role=role.value, tool=tool)
    try:
        found = check(
            cfg.envelope,
            role,
            tool,
            arguments,
            now=datetime.now(UTC),
            gated=cfg.tools[tool],
            take_caps=True,
            agents_table=cfg.agents_table,
            library_table=cfg.library_table,
        )
    except Exception as error:  # an unchecked call is refused, never let through
        logger.exception("the envelope could not be checked")
        found = Verdict(False, f"{tool}: the envelope could not be checked ({type(error).__name__})", Refusal.UNCHECKED)
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
    if found.allowed:
        try:
            _record(cfg, role, "agent.allow", tool, correlation_id=correlation_id, detail=_detail(tool, found, digest))
        except Exception as error:  # an unrecorded action is refused
            logger.exception("the allowed call could not be recorded")
            found = Verdict(
                False, f"{tool}: the call could not be recorded ({type(error).__name__})", Refusal.UNCHECKED
            )
        else:
            logger.info("agent call allowed", extra={"reason": found.reason})
            return passed(body)
    logger.warning("agent call refused", extra={"reason": found.reason, "refusal": str(found.refusal)})
    try:
        _record(cfg, role, "agent.refuse", tool, correlation_id=correlation_id, detail=_detail(tool, found, digest))
        escalate(cfg, role, tool, found, correlation_id)
    except Exception:  # the refusal stands even when it cannot be recorded or escalated
        logger.exception("the refusal could not be recorded or escalated")
    return refused(body, found.reason)
