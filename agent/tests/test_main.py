"""The AG-UI HTTP surface as AgentCore Runtime calls it: POST /invocations (SSE) and GET /ping."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

import fast_channels.agent.main as main
from fast_channels.agent.testing import OPERATOR_CLAIMS, Harness, kinds, text, unsigned_jwt


@pytest.fixture
def client(harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Harness]]:
    h = harness([text("Hello operator.")])
    monkeypatch.setattr(main, "scheduler", lambda: h.scheduler)
    with TestClient(main.app) as test_client:
        yield test_client, h


def sse(stream: str) -> list[dict[str, Any]]:
    return [json.loads(line.removeprefix("data: ")) for line in stream.splitlines() if line.startswith("data: ")]


def body(session_id: str) -> dict[str, Any]:
    return {
        "threadId": session_id,
        "runId": "r1",
        "state": {},
        "tools": [],
        "context": [],
        "messages": [{"id": "m1", "role": "user", "content": "hi"}],
        "forwardedProps": {},
    }


def test_invocation_streams_ag_ui_events_and_forwards_identity(client: tuple[TestClient, Harness]) -> None:
    test_client, h = client
    headers = {
        "Authorization": f"Bearer {unsigned_jwt(**OPERATOR_CLAIMS)}",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": h.session_id,
        "Accept": "text/event-stream",
    }

    response = test_client.post("/invocations", json=body(h.session_id), headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert kinds(sse(response.text)) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert h.gateway.callers[0].actor_id == "user-123"
    assert h.gateway.callers[0].authorization == headers["Authorization"]


def test_invalid_run_input_is_rejected_before_streaming(client: tuple[TestClient, Harness]) -> None:
    assert client[0].post("/invocations", json={"messages": []}).status_code == 400


def test_ping(client: tuple[TestClient, Harness]) -> None:
    assert client[0].get("/ping").json() == {"status": "Healthy"}
