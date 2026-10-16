from typing import Any

import pytest
from ag_ui.core import RunAgentInput

from fast_channels.agent.protocol import AgentRunError, Approval, Mode, SharedState, Turn


def run_input(**fields: Any) -> RunAgentInput:
    return RunAgentInput.model_validate({"threadId": "t", "runId": "r", "messages": [], **fields})


def test_reads_only_the_latest_user_message() -> None:
    turn = Turn.from_input(
        run_input(
            messages=[
                {"id": "1", "role": "user", "content": "old"},
                {"id": "2", "role": "assistant", "content": "answer"},
                {
                    "id": "3",
                    "role": "user",
                    "content": [{"type": "text", "text": "new"}, {"type": "text", "text": "line"}],
                },
            ]
        )
    )

    assert turn.prompt == "new\nline"
    assert turn.approvals == ()
    assert turn.mode is None


def test_context_is_rendered_as_a_block() -> None:
    turn = Turn.from_input(
        run_input(
            messages=[{"id": "1", "role": "user", "content": "hi"}],
            context=[{"description": "Viewing channel", "value": "fast-demo"}],
        )
    )

    assert turn.context == "<console_context>\n- Viewing channel: fast-demo\n</console_context>"


def test_forwarded_approval_and_mode() -> None:
    turn = Turn.from_input(
        run_input(
            forwardedProps={"approval": {"interrupt_id": "i-1", "approved": False, "note": "later"}, "mode": "plan"}
        )
    )

    assert turn.approvals == (Approval(interrupt_id="i-1", approved=False, note="later"),)
    assert turn.mode is Mode.PLAN
    assert turn.prompt is None


def test_native_ag_ui_resume_entries() -> None:
    turn = Turn.from_input(
        run_input(
            resume=[
                {"interruptId": "a", "status": "resolved", "payload": {"approved": True}},
                {"interruptId": "b", "status": "resolved", "payload": {"approved": False, "note": "no"}},
                {"interruptId": "c", "status": "cancelled"},
                {"interruptId": "d", "status": "resolved"},
            ]
        )
    )

    assert [(a.interrupt_id, a.approved, a.note) for a in turn.approvals] == [
        ("a", True, None),
        ("b", False, "no"),
        ("c", False, None),
        ("d", True, None),
    ]


def test_shared_state_round_trips() -> None:
    turn = Turn.from_input(
        run_input(
            messages=[{"id": "1", "role": "user", "content": "hi"}],
            state={"channel": "fast-demo", "plan": {"plan_id": "p"}},
        )
    )

    assert turn.state == SharedState(channel="fast-demo", plan={"plan_id": "p"})


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"messages": [{"id": "1", "role": "user", "content": "   "}]},
        {"forwardedProps": {"approval": {"interrupt_id": "x"}}},
        {"forwardedProps": {"mode": "turbo"}},
        {"messages": [{"id": "1", "role": "user", "content": "hi"}], "state": {"plan": "not-an-object"}},
    ],
)
def test_bad_requests(fields: dict[str, Any]) -> None:
    with pytest.raises(AgentRunError) as error:
        Turn.from_input(run_input(**fields))
    assert error.value.code == "BAD_REQUEST"
