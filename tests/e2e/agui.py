"""The AG-UI wire format of the scheduling agent (CONTRACTS §G): one ``RunAgentInput`` in, Server-Sent Events out."""

import json
from collections.abc import Iterable, Iterator
from typing import Any
from uuid import uuid4

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def session_id() -> str:
    """A new conversation id (AgentCore requires at least 33 characters)."""
    return f"fast-e2e-{uuid4()}"


def run_input(prompt: str, *, thread_id: str) -> dict[str, Any]:
    """``RunAgentInput`` carrying only the new user message (the runtime keeps the history)."""
    return {
        "threadId": thread_id,
        "runId": str(uuid4()),
        "messages": [{"id": str(uuid4()), "role": "user", "content": prompt}],
        "state": {},
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """AG-UI events of an SSE stream: the JSON of each event's ``data`` lines (other fields are ignored; an event
    the stream ends without terminating is discarded, as the SSE specification requires)."""
    data: list[str] = []
    for line in lines:
        if not line:
            if data:
                yield json.loads("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def tool_calls(stream: Iterable[dict[str, Any]]) -> list[str]:
    """Names of the tools the agent called, in order."""
    return [event.get("toolCallName", "") for event in stream if event.get("type") == "TOOL_CALL_START"]
