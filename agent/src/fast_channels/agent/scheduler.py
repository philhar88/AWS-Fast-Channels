"""One AG-UI run of the scheduling agent: identity → tools → Strands agent → AG-UI events.

The process holds no conversation state. Every run builds its agent from the session in AgentCore Memory
(history, active skills and pending approvals), so a recycled microVM resumes exactly where the last one stopped.
Static parts (prompt, skills, model clients, tool definitions) are cached for the life of the process.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Hashable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ag_ui.core import BaseEvent, RunAgentInput, RunErrorEvent, RunStartedEvent
from strands import Agent
from strands.models import Model
from strands.session import SessionManager
from strands.types.exceptions import MCPClientInitializationError, ModelThrottledException
from strands.vended_plugins.skills import AgentSkills, Skill

from fast_channels.agent.events import EventTranslator, approval_events, run_finished
from fast_channels.agent.gateway import ToolCatalog, gateway_tools
from fast_channels.agent.hooks import SharedStateTracker, ToolGuard, pending_approvals
from fast_channels.agent.identity import Caller
from fast_channels.agent.memory import session_manager
from fast_channels.agent.model import ModelSpec, ModelSpecSource, bedrock_model
from fast_channels.agent.protocol import AgentRunError, Mode, Turn

if TYPE_CHECKING:
    from strands.agent import AgentResult
    from strands.types.agent import AgentInput
    from strands.types.content import ContentBlock
    from strands.types.interrupt import InterruptResponseContent
    from strands.types.tools import AgentTool

    from fast_channels.agent.settings import Settings

logger = logging.getLogger(__name__)

PACKAGE_DIR: Final = Path(__file__).parent
AGENT_ID: Final = "scheduler"

type ToolOpener = Callable[[Caller], AbstractAsyncContextManager[Sequence[AgentTool]]]
type ModelFactory = Callable[[ModelSpec, Mode], Model]
type SessionFactory = Callable[[str, Caller], SessionManager]


@cache
def system_prompt() -> str:
    """The operator-assistant system prompt (``prompts/system.md``)."""
    return (PACKAGE_DIR / "prompts" / "system.md").read_text()


@cache
def skills() -> tuple[Skill, ...]:
    """Domain skills (``skills/*/SKILL.md``), disclosed progressively by the Strands AgentSkills plugin."""
    return tuple(Skill.from_directory(PACKAGE_DIR / "skills"))


class Scheduler:
    """Runs AG-UI turns of the FAST channel scheduling agent.

    Collaborators default to the real ones (Gateway over MCP, Bedrock, AgentCore Memory); tests and the
    conformance suite swap them.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        tools: ToolOpener | None = None,
        model: ModelFactory | None = None,
        sessions: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._specs = ModelSpecSource(settings)
        catalog = ToolCatalog()
        self._tools = tools or (
            lambda caller: gateway_tools(
                settings.gateway_url, caller.authorization, principal=_principal(caller), catalog=catalog
            )
        )
        self._model = model or (lambda spec, mode: bedrock_model(spec, mode, settings.region))
        self._sessions = sessions or (
            lambda session_id, caller: session_manager(settings, session_id=session_id, actor_id=caller.actor_id)
        )

    async def run(
        self, run_input: RunAgentInput, *, session_id: str | None, headers: Mapping[str, str] | None
    ) -> AsyncIterator[BaseEvent]:
        """Stream the AG-UI events of one run; failures end it with RUN_ERROR instead of raising."""
        yield RunStartedEvent(thread_id=run_input.thread_id, run_id=run_input.run_id)
        try:
            async for event in self._turn(run_input, session_id or run_input.thread_id, headers):
                yield event
        except AgentRunError as error:
            logger.warning("run rejected", extra={"code": error.code, "error": error.message})
            yield RunErrorEvent(code=error.code, message=error.message)
        except Exception as error:
            code, message = _failure(error)
            logger.exception("run failed", extra={"code": code})
            yield RunErrorEvent(code=code, message=message)

    async def _turn(
        self, run_input: RunAgentInput, session_id: str, headers: Mapping[str, str] | None
    ) -> AsyncIterator[BaseEvent]:
        caller = Caller.from_headers(headers)
        turn = Turn.from_input(run_input)
        mode = turn.mode or (Mode.PLAN if caller.machine else Mode.CHAT)
        spec = self._specs.current()
        logger.info(
            "run started",
            extra={
                "correlation_id": session_id,
                "actor": caller.actor_id,
                "mode": mode.value,
                "model_id": spec.model_id,
            },
        )
        tracker = SharedStateTracker(turn.state)
        guard = ToolGuard(correlation_id=session_id, confirm_tools=self._settings.confirm_tools)
        async with self._tools(caller) as tools:

            def build() -> Agent:
                return Agent(
                    model=self._model(spec, mode),
                    system_prompt=system_prompt(),
                    tools=[*tools],
                    plugins=[AgentSkills(skills=list(skills()))],
                    hooks=[guard, tracker],
                    session_manager=self._sessions(session_id, caller),
                    agent_id=AGENT_ID,
                    name="fast-channels-scheduler",
                    callback_handler=None,
                    trace_attributes={"session.id": session_id, "actor.id": caller.actor_id},
                )

            # Restoring the session from Memory makes blocking boto3 calls: build the agent off the event loop.
            agent = await asyncio.to_thread(build)
            translator = EventTranslator(tracker.take)
            result: AgentResult | None = None
            async for event in agent.stream_async(_agent_input(agent, turn)):
                result = event.get("result", result)
                for translated in translator.translate(event):
                    yield translated
            for translated in translator.close():
                yield translated
        if result is not None and result.interrupts:
            for approval in approval_events(result.interrupts):
                yield approval
        yield run_finished(
            thread_id=run_input.thread_id, run_id=run_input.run_id, result=result, model_id=spec.model_id
        )


def _agent_input(agent: Agent, turn: Turn) -> AgentInput:
    """Strands input for the turn: interrupt responses when approvals are pending, otherwise the message."""
    pending = pending_approvals(agent)
    if turn.approvals:
        if unknown := [a.interrupt_id for a in turn.approvals if a.interrupt_id not in pending]:
            raise AgentRunError("APPROVAL_NOT_FOUND", f"No pending approval with id {', '.join(unknown)}.")
        answers = {a.interrupt_id: {"approved": a.approved, "note": a.note} for a in turn.approvals}
    elif pending:
        # A new message instead of a decision declines what is pending; the model reads the message as the reason.
        answers = {interrupt_id: {"approved": False, "note": turn.prompt} for interrupt_id in pending}
    else:
        blocks: list[ContentBlock] = [{"text": text} for text in (turn.prompt, turn.context) if text]
        return blocks
    responses: list[InterruptResponseContent] = [
        {"interruptResponse": {"interruptId": key, "response": value}} for key, value in answers.items()
    ]
    return responses


def _principal(caller: Caller) -> Hashable:
    return caller.actor_id, caller.client_id, caller.groups


def _failure(error: Exception) -> tuple[str, str]:
    match error:
        case ModelThrottledException():
            return "THROTTLED", "The model is throttling requests; retry shortly."
        case MCPClientInitializationError():
            return "GATEWAY_UNAVAILABLE", "The tool gateway could not be reached."
        case _:
            return "INTERNAL_ERROR", f"{type(error).__name__}: {error}"[:500]
