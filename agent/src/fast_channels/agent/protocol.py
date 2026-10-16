"""The agent's side of the AG-UI contract with the console (CONTRACTS.md §G).

Inbound: an AG-UI ``RunAgentInput``. The server keeps the conversation (AgentCore Memory, keyed by session), so
only the newest user message is read; earlier ``messages`` the client replays are ignored. A run resumes a paused
approval with ``forwardedProps.approval`` (contract) or with standard AG-UI ``resume`` entries.

Outbound value shapes: the ``approval_required`` CUSTOM event, the ``STATE_SNAPSHOT`` state, and the error envelope
that every failed ``TOOL_CALL_RESULT`` carries (AG-UI results have no status field).
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Self

from ag_ui.core import Context, RunAgentInput, TextPart, UserMessage
from pydantic import BaseModel, ValidationError

APPROVAL_EVENT: Final = "approval_required"
"""Name of the AG-UI CUSTOM event that asks the operator to approve a CONFIRM tool call."""
DECLINED: Final = "DECLINED"
"""Error code of a CONFIRM tool call the operator declined (the call never ran)."""
TOOL_ERROR: Final = "TOOL_ERROR"
"""Error code of a failed call that did not return a tool envelope: a Cedar denial, a Gateway, MCP or Strands error."""


def error_envelope(code: str, message: str, *, hint: str = "") -> str:
    """JSON text of the tools' error envelope (SPEC §6), for failures the agent reports itself; never retryable."""
    return json.dumps({"ok": False, "error": {"code": code, "message": message, "retryable": False, "hint": hint}})


class AgentRunError(Exception):
    """A failure reported to the client as ``RUN_ERROR {code, message}``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Mode(StrEnum):
    """Reasoning effort profile of a run (ModelSpec ``effort.chat`` / ``effort.plan``)."""

    CHAT = "chat"
    PLAN = "plan"


class Approval(BaseModel, frozen=True):
    """The operator's decision on one pending approval (``forwardedProps.approval``)."""

    interrupt_id: str
    approved: bool
    note: str | None = None


class ApprovalRequest(BaseModel, frozen=True):
    """Value of the ``approval_required`` CUSTOM event (without ``interrupt_id``, which Strands assigns)."""

    tool: str
    arguments: dict[str, Any]
    reason: str
    tool_call_id: str


class ScheduleScope(StrEnum):
    """How much of the channel's guide ``SharedState.schedule`` holds."""

    CHANNEL = "channel"
    """The guide from now on (``schedule___get_schedule``), with later publishes and removals folded in."""
    PUBLISHED = "published"
    """Only the programs published in this conversation: the guide was not read before the first publish."""


class SharedState(BaseModel):
    """The AG-UI shared state the agent publishes in ``STATE_SNAPSHOT`` after schedule tools.

    ``schedule`` is EPG-shaped (CONTRACTS §I: ``channel``, ``generated_at``, ``ladder_version``, ``programs``);
    ``schedule_scope`` says whether it is the channel's whole guide or only what this conversation published.
    """

    channel: str | None = None
    plan: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    schedule_scope: ScheduleScope | None = None


@dataclass(frozen=True, slots=True)
class Turn:
    """What the client asked for in one run: a new message, or decisions on pending approvals."""

    prompt: str | None
    context: str | None
    approvals: tuple[Approval, ...]
    mode: Mode | None
    state: SharedState

    @classmethod
    def from_input(cls, run_input: RunAgentInput) -> Self:
        """Parse a run; raises ``AgentRunError("BAD_REQUEST")`` when it carries neither text nor a decision."""
        props = run_input.forwarded_props if isinstance(run_input.forwarded_props, dict) else {}
        try:
            approvals = tuple(_approvals(props.get("approval"), run_input))
            mode = Mode(props["mode"]) if "mode" in props else None
            state = SharedState.model_validate(run_input.state or {})
        except (ValidationError, ValueError, TypeError) as error:
            raise AgentRunError("BAD_REQUEST", f"Invalid run input: {error}") from error
        prompt = _latest_user_text(run_input)
        if not approvals and not prompt:
            raise AgentRunError("BAD_REQUEST", "The run carries no user message and no approval decision.")
        return cls(
            prompt=prompt, context=_context(run_input.context or []), approvals=approvals, mode=mode, state=state
        )


def _approvals(forwarded: object, run_input: RunAgentInput) -> list[Approval]:
    approvals = [Approval.model_validate(forwarded)] if forwarded is not None else []
    for entry in run_input.resume or []:
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        approvals.append(
            Approval(
                interrupt_id=entry.interrupt_id,
                approved=entry.status == "resolved" and payload.get("approved", True) is True,
                note=payload.get("note"),
            )
        )
    return approvals


def _latest_user_text(run_input: RunAgentInput) -> str | None:
    # Only text parts are read: structured content from the client never reaches the model as a toolUse
    # block (Runtime does not filter injected toolUse payloads; agentcore.md §4.2).
    message = next((m for m in reversed(run_input.messages) if isinstance(m, UserMessage)), None)
    if message is None:
        return None
    content = message.content
    text = content if isinstance(content, str) else "\n".join(p.text for p in content if isinstance(p, TextPart))
    return text.strip() or None


def _context(entries: list[Context]) -> str | None:
    if not entries:
        return None
    lines = "\n".join(f"- {entry.description}: {entry.value}" for entry in entries)
    return f"<console_context>\n{lines}\n</console_context>"
