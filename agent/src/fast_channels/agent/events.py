"""Translation of a Strands agent stream into standard AG-UI events."""

import json
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    Interrupt,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    TokenUsage,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from fast_channels.agent.hooks import result_payload
from fast_channels.agent.protocol import APPROVAL_EVENT, TOOL_ERROR, error_envelope

if TYPE_CHECKING:
    from strands.agent import AgentResult
    from strands.interrupt import Interrupt as StrandsInterrupt
    from strands.types.tools import ToolResult

type Snapshots = Callable[[str], dict[str, Any] | None]


class EventTranslator:
    """Maps one run's Strands stream events to AG-UI events.

    Text deltas become a TEXT_MESSAGE_START/CONTENT/END sequence, streamed tool uses become
    TOOL_CALL_START/ARGS/END, and the tool-result message becomes TOOL_CALL_RESULT (see ``result_content``), each
    followed by the STATE_SNAPSHOT its tool produced (``snapshots``). Reasoning is not forwarded.
    """

    def __init__(self, snapshots: Snapshots) -> None:
        self._snapshots = snapshots
        self._text_id: str | None = None
        self._open_calls: list[str] = []
        self._started_calls: set[str] = set()

    def translate(self, event: Mapping[str, Any]) -> Iterator[BaseEvent]:
        """AG-UI events for one Strands stream event (most produce none)."""
        match event:
            case {"data": str() as text} if text:
                if self._text_id is None:
                    self._text_id = _new_id()
                    yield TextMessageStartEvent(message_id=self._text_id, role="assistant")
                yield TextMessageContentEvent(message_id=self._text_id, delta=text)
            case {"current_tool_use": {"toolUseId": str() as call_id, "name": str() as name}}:
                yield from self._end_text()
                if call_id not in self._started_calls:
                    self._started_calls.add(call_id)
                    self._open_calls.append(call_id)
                    yield ToolCallStartEvent(tool_call_id=call_id, tool_call_name=name)
                if chunk := event.get("delta", {}).get("toolUse", {}).get("input"):
                    yield ToolCallArgsEvent(tool_call_id=call_id, delta=chunk)
            case {"message": {"role": "assistant"}}:
                yield from self.close()
            case {"message": {"role": "user", "content": list() as blocks}}:
                for block in blocks:
                    if result := block.get("toolResult"):
                        yield from self._result(result)

    def close(self) -> Iterator[BaseEvent]:
        """End any message or tool call still open (end of an assistant message or of the run)."""
        yield from self._end_text()
        for call_id in self._open_calls:
            yield ToolCallEndEvent(tool_call_id=call_id)
        self._open_calls.clear()

    def _end_text(self) -> Iterator[BaseEvent]:
        if self._text_id is not None:
            yield TextMessageEndEvent(message_id=self._text_id)
            self._text_id = None

    def _result(self, result: ToolResult) -> Iterator[BaseEvent]:
        call_id = result["toolUseId"]
        content = result_content(result)
        yield ToolCallResultEvent(message_id=_new_id(), tool_call_id=call_id, content=content, role="tool")
        if (snapshot := self._snapshots(call_id)) is not None:
            yield StateSnapshotEvent(snapshot=snapshot)


def approval_events(interrupts: Sequence[StrandsInterrupt]) -> Iterator[CustomEvent]:
    """One ``approval_required`` CUSTOM event per pending approval (CONTRACTS.md §G)."""
    for interrupt in interrupts:
        yield CustomEvent(name=APPROVAL_EVENT, value={"interrupt_id": interrupt.id, **(interrupt.reason or {})})


def run_finished(*, thread_id: str, run_id: str, result: AgentResult | None, model_id: str) -> RunFinishedEvent:
    """RUN_FINISHED with the standard interrupt outcome when the run paused for approval, and token usage."""
    interrupts = list(result.interrupts or ()) if result else []
    outcome = (
        RunFinishedInterruptOutcome(
            interrupts=[
                Interrupt(
                    id=i.id,
                    reason=APPROVAL_EVENT,
                    message=(i.reason or {}).get("reason"),
                    tool_call_id=(i.reason or {}).get("tool_call_id"),
                )
                for i in interrupts
            ]
        )
        if interrupts
        else None
    )
    return RunFinishedEvent(
        thread_id=thread_id, run_id=run_id, outcome=outcome, usage=[_usage(result, model_id)] if result else None
    )


def result_content(result: ToolResult) -> str:
    """TOOL_CALL_RESULT content: the result's text, and for every failed call an ``{"ok": false}`` envelope.

    AG-UI results carry no status, so clients tell a failure by the envelope tools return (SPEC §6). Failures that
    never reached a tool, such as a Cedar denial or a Gateway, MCP or Strands error, come back as plain text with
    status ``error``; they are wrapped as ``TOOL_ERROR``. A declined approval already is a ``DECLINED`` envelope.
    """
    text = result_text(result)
    payload = result_payload(result) or {}
    if result.get("status") != "error" or payload.get("ok") is False:
        return text
    return error_envelope(TOOL_ERROR, text or "The tool call failed.")


def result_text(result: ToolResult) -> str:
    """A tool result's content as text (JSON blocks serialised)."""
    return "\n".join(
        block["text"] if "text" in block else json.dumps(block["json"])
        for block in result.get("content", [])
        if "text" in block or "json" in block
    )


def _usage(result: AgentResult, model_id: str) -> TokenUsage:
    usage = result.metrics.accumulated_usage
    cached, written = usage.get("cacheReadInputTokens", 0), usage.get("cacheWriteInputTokens", 0)
    # AG-UI counts cache reads/writes inside input_tokens; Bedrock reports them separately.
    input_tokens = usage.get("inputTokens", 0) + cached + written
    return TokenUsage(
        provider="bedrock",
        model=model_id,
        input_tokens=input_tokens,
        output_tokens=usage.get("outputTokens", 0),
        total_tokens=input_tokens + usage.get("outputTokens", 0),
        cached_input_tokens=cached,
        cache_write_input_tokens=written,
    )


def _new_id() -> str:
    return str(uuid.uuid4())
