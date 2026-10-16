"""Test doubles for the scheduling agent: a scripted model, Gateway-shaped tools and a turn harness.

Used by the package's unit tests and usable by anyone who customises the agent: everything except Bedrock and the
Gateway is real (Strands loop, hooks, approvals, AG-UI translation, session persistence on local disk).
"""

import base64
import copy
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ag_ui.core import RunAgentInput
from strands.models import Model
from strands.session import FileSessionManager
from strands.tools.tools import PythonAgentTool

from fast_channels.agent.scheduler import Scheduler

if TYPE_CHECKING:
    from strands.types.tools import ToolSpec, ToolUse

    from fast_channels.agent.identity import Caller
    from fast_channels.agent.model import ModelSpec
    from fast_channels.agent.protocol import Mode
    from fast_channels.agent.settings import Settings

type Block = dict[str, Any]
type Handler = Callable[[dict[str, Any]], dict[str, Any]]
type Event = dict[str, Any]


def unsigned_jwt(**claims: Any) -> str:
    """A JWT-shaped token with ``claims`` (the Runtime verifies signatures before agent code runs)."""

    def segment(value: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.c2ln"


def text(value: str) -> Block:
    """A scripted text block."""
    return {"text": value}


def tool_use(tool_use_id: str, name: str, arguments: dict[str, Any]) -> Block:
    """A scripted tool-use block."""
    return {"tool_use": (tool_use_id, name, arguments)}


class ScriptedModel(Model):
    """Replays scripted assistant turns as Bedrock ConverseStream events and records every request.

    ``turns`` is consumed one entry per model call; share the list between instances to script a conversation
    that spans several runs.
    """

    def __init__(self, turns: list[list[Block]]) -> None:
        self.turns = turns
        self.calls: list[dict[str, Any]] = []

    def update_config(self, **model_config: Any) -> None:
        """Nothing to configure."""

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        raise NotImplementedError
        yield {}

    async def stream(  # type: ignore[override]
        self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(
            {"messages": copy.deepcopy(messages), "tool_specs": tool_specs, "system_prompt": system_prompt}
        )
        blocks = self.turns.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        for block in blocks:
            if "text" in block:
                yield {"contentBlockDelta": {"delta": {"text": block["text"]}}}
            else:
                tool_use_id, name, arguments = block["tool_use"]
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
            yield {"contentBlockStop": {}}
        stop = "tool_use" if any("tool_use" in block for block in blocks) else "end_turn"
        yield {"messageStop": {"stopReason": stop}}
        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "totalTokens": 15,
                    "cacheReadInputTokens": 2,
                    "cacheWriteInputTokens": 1,
                },
                "metrics": {"latencyMs": 1},
            }
        }


@dataclass
class FakeGateway:
    """Gateway-shaped tools: ``<target>___<tool>`` names and JSON text results, recording every call."""

    handlers: dict[str, Handler]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    callers: list[Caller] = field(default_factory=list)

    @asynccontextmanager
    async def open(self, caller: Caller) -> AsyncIterator[list[PythonAgentTool]]:
        """Tool opener for :class:`Scheduler` (one "connection" per run)."""
        self.callers.append(caller)
        yield [self._tool(name, handler) for name, handler in self.handlers.items()]

    def _tool(self, name: str, handler: Handler) -> PythonAgentTool:
        def run(tool_use: ToolUse, **_: Any) -> dict[str, Any]:
            arguments = dict(tool_use["input"])
            self.calls.append((name, arguments))
            return {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": json.dumps(handler(arguments))}],
            }

        spec: ToolSpec = {
            "name": name,
            "description": f"{name.replace('___', ' ')}. Second sentence.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
        return PythonAgentTool(name, spec, run)  # type: ignore[arg-type]


@dataclass
class Harness:
    """A :class:`Scheduler` wired to a scripted model, fake tools and on-disk sessions."""

    scheduler: Scheduler
    gateway: FakeGateway
    models: list[tuple[Mode, ScriptedModel]]
    session_id: str

    @classmethod
    def build(
        cls,
        settings: Settings,
        sessions_dir: Path,
        script: Sequence[Sequence[Block]],
        handlers: dict[str, Handler],
        session_id: str,
    ) -> Harness:
        """Every run builds a model that consumes the shared ``script``, like one model across turns."""
        gateway = FakeGateway(handlers)
        turns = [list(turn) for turn in script]
        models: list[tuple[Mode, ScriptedModel]] = []

        def model(_spec: ModelSpec, mode: Mode) -> ScriptedModel:
            models.append((mode, ScriptedModel(turns)))
            return models[-1][1]

        def sessions(session: str, _caller: Caller) -> FileSessionManager:
            return FileSessionManager(session_id=session, storage_dir=str(sessions_dir))

        scheduler = Scheduler(settings, tools=gateway.open, model=model, sessions=sessions)
        return cls(scheduler=scheduler, gateway=gateway, models=models, session_id=session_id)

    async def run(
        self,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        claims: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> list[Event]:
        """Run one turn and return its AG-UI events as wire-format dicts.

        ``fields`` are extra ``RunAgentInput`` fields (``resume``, ``context``, ...), except ``forwarded_props``
        keys, which are given directly: ``run(approval={...})``.
        """
        input_fields = {k: fields.pop(k) for k in ("resume", "context", "state") if k in fields}
        run_input = RunAgentInput(
            thread_id=self.session_id,
            run_id=str(uuid.uuid4()),
            messages=[{"id": "m1", "role": "user", "content": message}] if message else [],  # type: ignore[list-item]
            forwarded_props=fields,
            **input_fields,
        )
        if headers is None:
            headers = {"Authorization": f"Bearer {unsigned_jwt(**(claims or OPERATOR_CLAIMS))}"}
        return [
            event.model_dump(mode="json", by_alias=True)
            async for event in self.scheduler.run(run_input, session_id=self.session_id, headers=headers)
        ]


OPERATOR_CLAIMS: Mapping[str, Any] = {
    "sub": "user-123",
    "client_id": "console",
    "username": "operator1",
    "cognito:groups": ["fast-operators"],
    "token_use": "access",
}
"""Claims of a Cognito access token issued to an operator through the console client."""
AUTOPILOT_CLAIMS: Mapping[str, Any] = {
    "sub": "autopilot-client",
    "client_id": "autopilot-client",
    "token_use": "access",
    "scope": "fast-channels/agent.invoke",
}
"""Claims of a client-credentials (machine) token, as Autopilot sends."""


def kinds(events: Sequence[Event]) -> list[str]:
    """The ``type`` of each event."""
    return [event["type"] for event in events]


def of_kind(events: Sequence[Event], kind: str) -> list[Event]:
    """The events of one ``type``."""
    return [event for event in events if event["type"] == kind]
