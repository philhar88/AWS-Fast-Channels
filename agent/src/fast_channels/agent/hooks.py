"""Strands hooks around Gateway tool calls: operator approval, correlation id, shared schedule state."""

import json
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry
from strands.interrupt import InterruptException

from fast_channels.agent.gateway import CORRELATION_FIELD, is_gateway_tool
from fast_channels.agent.protocol import DECLINED, ApprovalRequest, ScheduleScope, SharedState, error_envelope

if TYPE_CHECKING:
    from strands.agent import Agent
    from strands.types.tools import ToolResult, ToolUse

PENDING_APPROVALS_KEY: Final = "fast_pending_approvals"
"""``agent.state`` key: approvals the operator still has to answer, by interrupt id (persisted with the session)."""
APPROVAL_INTERRUPT: Final = "approval"


class ToolGuard:
    """Before every Gateway tool call: ask the operator when the tool is CONFIRM, then stamp the correlation id.

    CONFIRM tools raise a Strands interrupt; the run ends with an ``approval_required`` event and resumes when the
    client answers. The tool use that was shown is the one that runs: resuming re-enters this hook with the
    same tool use id and only the answer added. A declined call is cancelled with a ``DECLINED`` error envelope, so
    the model reads why and the console shows the call as declined.
    """

    def __init__(self, *, correlation_id: str, confirm_tools: frozenset[str]) -> None:
        self._correlation_id = correlation_id
        self._confirm_tools = confirm_tools

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_use = event.tool_use
        if not is_gateway_tool(tool_use["name"]):
            return
        if tool_use["name"] in self._confirm_tools and not self._approved(event):
            return
        # A new dict: the model's own tool use (kept in history) stays free of runtime fields.
        event.tool_use = {**tool_use, "input": {**_arguments(tool_use), CORRELATION_FIELD: self._correlation_id}}

    def _approved(self, event: BeforeToolCallEvent) -> bool:
        tool_use = event.tool_use
        request = ApprovalRequest(
            tool=tool_use["name"],
            arguments=_arguments(tool_use),
            reason=_purpose(event),
            tool_call_id=tool_use["toolUseId"],
        )
        try:
            answer = event.interrupt(APPROVAL_INTERRUPT, reason=request.model_dump())
        except InterruptException as raised:
            _set_pending(event.agent, {**pending_approvals(event.agent), raised.interrupt.id: request})
            raise
        _set_pending(
            event.agent,
            {
                interrupt_id: pending
                for interrupt_id, pending in pending_approvals(event.agent).items()
                if pending.tool_call_id != request.tool_call_id
            },
        )
        if isinstance(answer, dict) and answer.get("approved") is True:
            return True
        note = answer.get("note") if isinstance(answer, dict) else None
        event.cancel_tool = error_envelope(
            DECLINED,
            f"The operator declined {request.tool}" + (f": {note}" if note else "."),
            hint="Do not retry this call; ask the operator what they want instead.",
        )
        return False


class SharedStateTracker:
    """After schedule tools: fold the result into the AG-UI shared state and queue a ``STATE_SNAPSHOT``.

    Snapshots come from tool results, never from model text, so the console's schedule view only shows what the
    tools computed. A publish or removal result covers only the programs it changed, so it is folded into the
    guide already shown instead of replacing it (``SharedState.schedule_scope`` says what that guide covers).
    """

    def __init__(self, state: SharedState) -> None:
        self.state = state
        self._snapshots: dict[str, dict[str, Any]] = {}

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool_call)

    def take(self, tool_call_id: str) -> dict[str, Any] | None:
        """The snapshot produced by ``tool_call_id``, if any (each snapshot is handed out once)."""
        return self._snapshots.pop(tool_call_id, None)

    def _after_tool_call(self, event: AfterToolCallEvent) -> None:
        name = event.tool_use["name"]
        payload = result_payload(event.result)
        if name not in _SNAPSHOT_TOOLS or payload is None or payload.get("ok") is False:
            return
        channel = payload.get("channel") or _arguments(event.tool_use).get("channel") or self.state.channel
        current = self.state if channel == self.state.channel else SharedState()
        if (update := _state_update(name, current, payload)) is None:
            return
        self.state = current.model_copy(update={"channel": channel, **update})
        self._snapshots[event.tool_use["toolUseId"]] = self.state.model_dump(mode="json")


_SNAPSHOT_TOOLS: Final = frozenset(
    {"schedule___plan", "schedule___publish", "schedule___get_schedule", "schedule___remove_programs"}
)
_GUIDE_FIELDS: Final = ("channel", "generated_at", "ladder_version")


def _state_update(tool: str, state: SharedState, result: dict[str, Any]) -> dict[str, Any] | None:
    """The shared-state fields that ``result`` of ``tool`` changes; ``None`` when it changes nothing shown."""
    match tool:
        case "schedule___plan":
            return {"plan": result}
        case "schedule___get_schedule":
            return {
                "schedule": fold_guide(None, result, upserted=result.get("programs", ())),
                "schedule_scope": ScheduleScope.CHANNEL,
            }
        case "schedule___publish":
            shown = state.schedule_scope if state.schedule is not None else None
            return {
                "plan": None,  # the plan is now on air
                "schedule": fold_guide(state.schedule, result, upserted=result.get("programs", ())),
                "schedule_scope": shown or ScheduleScope.PUBLISHED,
            }
        case "schedule___remove_programs" if state.schedule is not None:
            moved, removed = result.get("moved", ()), result.get("removed", ())
            return {"schedule": fold_guide(state.schedule, result, upserted=moved, removed=removed)}
        case _:
            return None


def fold_guide(
    guide: Mapping[str, Any] | None,
    change: Mapping[str, Any],
    *,
    upserted: Iterable[Mapping[str, Any]] = (),
    removed: Iterable[str] = (),
) -> dict[str, Any]:
    """``guide`` (EPG-shaped, CONTRACTS §I) after the schedule change ``change``, programs in air order.

    ``upserted`` programs replace those of the same name or join them, and ``removed`` names leave. The guide
    fields (channel, generated_at, ladder_version) that ``change`` carries win over those of ``guide``.
    """
    new = [dict(p) for p in upserted]
    gone = {*removed, *(p.get("name") for p in new)}
    kept = [p for p in _programs(guide) if p.get("name") not in gone]
    fields = {k: v for source in (guide or {}, change) for k, v in source.items() if k in _GUIDE_FIELDS}
    return {**fields, "programs": sorted([*kept, *new], key=_air_order)}


def _programs(guide: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The guide's programs; the guide comes back from the client, so anything that is not a program is dropped."""
    programs = (guide or {}).get("programs")
    return [p for p in programs if isinstance(p, dict)] if isinstance(programs, list) else []


def _air_order(program: Mapping[str, Any]) -> tuple[str, str]:
    # Tools format every time the same way (UTC ISO-8601 with milliseconds), so text order is time order.
    return str(program.get("start", "")), str(program.get("name", ""))


def pending_approvals(agent: Agent) -> dict[str, ApprovalRequest]:
    """Approvals still awaiting an answer in this session, by interrupt id."""
    stored = agent.state.get(PENDING_APPROVALS_KEY) or {}
    return {interrupt_id: ApprovalRequest.model_validate(value) for interrupt_id, value in stored.items()}


def result_payload(result: ToolResult) -> dict[str, Any] | None:
    """The JSON object a tool returned (Gateway results arrive as a JSON text block)."""
    for block in result.get("content", []):
        value: Any = block.get("json")
        if value is None and "text" in block:
            try:
                value = json.loads(block["text"])
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _set_pending(agent: Agent, pending: dict[str, ApprovalRequest]) -> None:
    agent.state.set(PENDING_APPROVALS_KEY, {key: value.model_dump() for key, value in pending.items()})


def _arguments(tool_use: ToolUse) -> dict[str, Any]:
    arguments = tool_use.get("input")
    return dict(arguments) if isinstance(arguments, dict) else {}


def _purpose(event: BeforeToolCallEvent) -> str:
    """First sentence of the tool's own description: what the operator is approving."""
    description = event.selected_tool.tool_spec.get("description", "") if event.selected_tool else ""
    return description.split(". ", 1)[0].strip().rstrip(".") + "." if description else event.tool_use["name"]
