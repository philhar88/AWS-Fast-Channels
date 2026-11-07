"""The Gateway's request interceptor: the envelope enforced on the validated token, whoever calls the Gateway.

There is no runner in these tests: each request is what the Gateway hands the interceptor for a tool call made with an
agent's machine token, as a caller holding that token could make it directly.
"""

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.agents.envelope import Envelope, Role
from fast_channels.functions import aws
from fast_channels.functions.agents import interceptor

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agent_tables import AgentTables

TOPIC = "arn:aws:sns:us-east-1:123456789012:fast-alerts"
START = "channels___start_channel"
PUBLISH = "schedule___publish"
TOOLS = {
    START: True,
    PUBLISH: True,
    "schedule___plan": False,
    "channels___stop_channel": True,
    "sales___propose": False,
}
CLIENTS = {"ops-client": Role.OPERATIONS, "prog-client": Role.PROGRAMMING, "sales-client": Role.SALES}


def envelope() -> Envelope:
    agents = Envelope().agents
    on = {role: agent.model_copy(update={"enabled": True}) for role, agent in agents.items()}
    return Envelope(always_on=("{prefix}-demo",), programmed=("{prefix}-demo",), agents=on).resolved("fast")


@dataclass
class Context:
    aws_request_id: str = "request-1"
    function_name: str = "fast-interceptor"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fast-interceptor"
    log_group_name: str = "/aws/lambda/fast-interceptor"
    log_stream_name: str = "stream"


@dataclass
class Topic:
    published: list[dict[str, Any]] = field(default_factory=list)

    def publish(self, **message: Any) -> dict[str, str]:
        self.published.append(message)
        return {"MessageId": "1"}


def token(client_id: str) -> str:
    """A JWT-shaped bearer token (the Gateway validated its signature before the interceptor runs)."""
    claims = base64.urlsafe_b64encode(json.dumps({"client_id": client_id, "token_use": "access"}).encode())
    return f"eyJhbGciOiJSUzI1NiJ9.{claims.decode().rstrip('=')}.c2ln"


def call(tool: str, arguments: dict[str, Any], *, client_id: str = "ops-client", method: str = "tools/call") -> dict:
    body = {"jsonrpc": "2.0", "id": 7, "method": method, "params": {"name": tool, "arguments": arguments}}
    headers = {"Authorization": f"Bearer {token(client_id)}", "Mcp-Session-Id": "s"}
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {"gatewayRequest": {"path": "/mcp", "headers": headers, "body": body}},
    }


@pytest.fixture
def gateway(agent_tables: AgentTables, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[AgentTables, Topic]]:
    topic = Topic()
    monkeypatch.setattr(aws, "client", lambda service: topic if service == "sns" else agent_tables.client)
    monkeypatch.setattr(aws, "table", agent_tables.table)
    cfg = interceptor.InterceptorSettings(
        envelope=envelope(),
        clients=CLIENTS,
        tools=TOOLS,
        agents_table=agent_tables.agents,
        library_table=agent_tables.library,
        ledger_table=agent_tables.ledger,
        alert_topic=TOPIC,
        prefix="fast",
    )
    monkeypatch.setattr(interceptor, "interceptor_settings", lambda: cfg)
    yield agent_tables, topic


def passed(response: dict[str, Any]) -> bool:
    return "transformedGatewayRequest" in response["mcp"] and "transformedGatewayResponse" not in response["mcp"]


def refusal(response: dict[str, Any]) -> dict[str, Any]:
    answer = response["mcp"]["transformedGatewayResponse"]
    assert answer["statusCode"] == 200 and answer["body"]["id"] == 7 and answer["body"]["result"]["isError"]
    [block] = answer["body"]["result"]["content"]
    error: dict[str, Any] = json.loads(block["text"])["error"]
    return error


def actions(tables: AgentTables) -> list[tuple[str, str, dict[str, Any]]]:
    entries = sorted(tables.ledger_entries(), key=lambda e: e["sk"])
    return [(e["action"], e["principal"], json.loads(e["detail"])) for e in entries]


def test_a_machine_client_calling_the_gateway_directly_is_capped(gateway: tuple[AgentTables, Topic]) -> None:
    """No runner: the operations agent's token starts the always-on channel twice in an hour; the second is refused
    before it reaches the tool, recorded and escalated."""
    tables, topic = gateway

    first = interceptor.handler(call(START, {"channel": "fast-demo", "correlation_id": "c-1"}), Context())
    second = interceptor.handler(call(START, {"channel": "fast-demo", "correlation_id": "c-2"}), Context())

    assert passed(first) and first["mcp"]["transformedGatewayRequest"]["body"]["params"]["name"] == START
    error = refusal(second)
    assert error["code"] == interceptor.REFUSED_CODE and "cap of 1 an hour" in error["message"]
    assert [(action, principal) for action, principal, _ in actions(tables)] == [
        ("agent.allow", "agent-operations"),
        ("agent.refuse", "agent-operations"),
        ("agent.escalate", "agent-operations"),
    ]
    assert all(detail["source"] == "gateway" for _, _, detail in actions(tables))
    assert actions(tables)[1][2]["refusal"] == "cap"
    [message] = topic.published
    assert message["TopicArn"] == TOPIC and "The Gateway refused" in message["Message"]


def test_two_concurrent_gateway_calls_let_exactly_one_through(gateway: tuple[AgentTables, Topic]) -> None:
    tables, _ = gateway
    barrier = threading.Barrier(2)
    responses: list[dict[str, Any]] = []
    lock = threading.Lock()

    def restart(n: int) -> None:
        barrier.wait()
        response = interceptor.handler(call(START, {"channel": "fast-demo", "correlation_id": f"c-{n}"}), Context())
        with lock:
            responses.append(response)

    threads = [threading.Thread(target=restart, args=(n,)) for n in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(passed(r) for r in responses) == [False, True]
    assert max(tables.counts().values()) == 1


@pytest.mark.parametrize(
    ("ahead", "allowed"), [(timedelta(minutes=30), False), (timedelta(minutes=125), True), (None, False)]
)
def test_a_publish_reaches_the_tool_only_two_hours_ahead(
    gateway: tuple[AgentTables, Topic], ahead: timedelta | None, allowed: bool
) -> None:
    tables, _ = gateway
    if ahead is not None:
        start_ms = int((datetime.now(UTC) + ahead).timestamp() * 1000)
        tables.store_plan("fast-demo.0a1b", start_ms=start_ms, expires_at=int(time.time()) + 3600)

    response = interceptor.handler(call(PUBLISH, {"plan_id": "fast-demo.0a1b"}, client_id="prog-client"), Context())

    assert passed(response) is allowed
    if not allowed:
        assert "120 minutes ahead" in refusal(response)["message"]


def test_a_propose_only_agent_records_proposals_but_cannot_commit(gateway: tuple[AgentTables, Topic]) -> None:
    tables, _ = gateway

    proposal = interceptor.handler(call("sales___propose", {"line": "direct-1"}, client_id="sales-client"), Context())
    commit = interceptor.handler(call(START, {"channel": "fast-demo"}, client_id="sales-client"), Context())

    assert passed(proposal)
    assert "only proposes" in refusal(commit)["message"]
    assert [key.split("@")[0] for key in tables.counts()] == ["CAP#sales#sales___propose"]


@pytest.mark.parametrize(
    "request_",
    [
        call(START, {"channel": "fast-demo"}, client_id="console-client"),
        call("channels___get_channel", {"channel": "fast-demo"}),
        call(START, {"channel": "fast-demo"}, method="tools/list"),
        {"interceptorInputVersion": "1.0", "mcp": {"gatewayRequest": {"body": {"method": "initialize"}}}},
    ],
)
def test_operators_reads_and_other_methods_pass_without_a_check(
    gateway: tuple[AgentTables, Topic], request_: dict[str, Any]
) -> None:
    tables, topic = gateway

    response = interceptor.handler(request_, Context())

    assert (
        passed(response)
        and response["mcp"]["transformedGatewayRequest"]["body"] == request_["mcp"]["gatewayRequest"]["body"]
    )
    assert tables.ledger_entries() == [] and tables.counts() == {} and not topic.published


def test_an_agent_call_that_cannot_be_checked_is_refused(
    gateway: tuple[AgentTables, Topic], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("DynamoDB is unreachable")

    monkeypatch.setattr(interceptor, "check", broken)

    response = interceptor.handler(call(START, {"channel": "fast-demo"}), Context())

    assert "could not be checked (RuntimeError)" in refusal(response)["message"]


def test_a_token_without_claims_is_an_operator_and_passes() -> None:
    assert interceptor.claims_of({"authorization": "Bearer not-a-jwt"}) == {}
    assert interceptor.claims_of({}) == {}
    assert interceptor.claims_of({"Authorization": f"Bearer {token('ops-client')}"})["client_id"] == "ops-client"
