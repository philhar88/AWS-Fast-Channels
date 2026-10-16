import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from ag_ui.core import RunAgentInput
from strands.types.exceptions import MCPClientInitializationError

from fast_channels.agent.hooks import PENDING_APPROVALS_KEY, ToolGuard
from fast_channels.agent.protocol import AgentRunError, Mode
from fast_channels.agent.scheduler import Scheduler
from fast_channels.agent.testing import (
    AUTOPILOT_CLAIMS,
    OPERATOR_CLAIMS,
    Block,
    Event,
    Harness,
    kinds,
    of_kind,
    text,
    tool_use,
    unsigned_jwt,
)

if TYPE_CHECKING:
    from fast_channels.agent.identity import Caller
    from fast_channels.agent.settings import Settings

type HarnessFactory = Callable[..., Harness]


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def test_text_and_tool_events_map_to_ag_ui(harness: HarnessFactory) -> None:
    h = harness(
        [text("Checking. "), tool_use("t1", "schedule___plan", {"channel": "fast-demo", "picks": ["a"]})],
        [text("Planned.")],
    )

    events = run(h.run("plan it"))

    assert kinds(events) == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "STATE_SNAPSHOT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    start, args, result = (of_kind(events, k)[0] for k in ("TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_RESULT"))
    assert (start["toolCallId"], start["toolCallName"]) == ("t1", "schedule___plan")
    assert json.loads(args["delta"]) == {"channel": "fast-demo", "picks": ["a"]}  # the model's args only
    assert json.loads(result["content"])["plan_id"] == "plan-1"
    snapshot = of_kind(events, "STATE_SNAPSHOT")[0]["snapshot"]
    assert (snapshot["channel"], snapshot["plan"]["plan_id"], snapshot["schedule"]) == ("fast-demo", "plan-1", None)
    finished = events[-1]
    assert finished["threadId"] == h.session_id
    assert "outcome" not in finished
    assert finished["usage"][0]["inputTokens"] == 2 * (10 + 2 + 1)  # cache reads/writes count as input


def test_correlation_id_is_injected_and_kept_out_of_history(harness: HarnessFactory) -> None:
    h = harness([tool_use("t1", "catalog___search_content", {"text": "bunny"})], [text("Found it.")])

    run(h.run("find bunny"))

    assert h.gateway.calls == [("catalog___search_content", {"text": "bunny", "correlation_id": h.session_id})]
    history = h.models[0][1].calls[1]["messages"]
    model_tool_use = next(b["toolUse"] for m in history for b in m["content"] if "toolUse" in b)
    assert model_tool_use["input"] == {"text": "bunny"}


def test_local_tools_are_not_stamped() -> None:
    original = {"name": "skills", "toolUseId": "s", "input": {"skill_name": "scheduling"}}
    event = SimpleNamespace(tool_use=original, selected_tool=None)

    ToolGuard(correlation_id="c", confirm_tools=frozenset({"skills"}))._before_tool_call(event)  # type: ignore[arg-type]

    assert event.tool_use is original
    assert "correlation_id" not in original["input"]


def publish_script() -> list[Block]:
    return [text("Publishing plan-1."), tool_use("p1", "schedule___publish", {"plan_id": "plan-1"})]


def interrupt_id(events: list[Event]) -> str:
    return str(of_kind(events, "CUSTOM")[0]["value"]["interrupt_id"])


def test_confirm_tool_pauses_for_approval_without_running(harness: HarnessFactory) -> None:
    h = harness(publish_script())

    events = run(h.run("publish plan-1"))

    assert kinds(events)[-2:] == ["CUSTOM", "RUN_FINISHED"]
    custom = of_kind(events, "CUSTOM")[0]
    assert custom["name"] == "approval_required"
    value = custom["value"]
    assert value["interrupt_id"].startswith("v1:before_tool_call:p1:")
    assert {k: v for k, v in value.items() if k != "interrupt_id"} == {
        "tool": "schedule___publish",
        "arguments": {"plan_id": "plan-1"},
        "reason": "schedule publish.",
        "tool_call_id": "p1",
    }
    assert events[-1]["outcome"] == {
        "type": "interrupt",
        "interrupts": [
            {
                "id": value["interrupt_id"],
                "reason": "approval_required",
                "message": "schedule publish.",
                "toolCallId": "p1",
            }
        ],
    }
    assert h.gateway.calls == []
    assert "TOOL_CALL_RESULT" not in kinds(events)


def test_approved_call_runs_on_resume(harness: HarnessFactory) -> None:
    h = harness(publish_script(), [text("Published.")])
    pending = interrupt_id(run(h.run("publish plan-1")))

    events = run(h.run(approval={"interrupt_id": pending, "approved": True}))

    assert h.gateway.calls == [("schedule___publish", {"plan_id": "plan-1", "correlation_id": h.session_id})]
    assert kinds(events) == [
        "RUN_STARTED",
        "TOOL_CALL_RESULT",
        "STATE_SNAPSHOT",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    snapshot = of_kind(events, "STATE_SNAPSHOT")[0]["snapshot"]
    assert snapshot["plan"] is None
    # No guide was read in this conversation, so the snapshot is only what was just published.
    assert snapshot["schedule"] == {"channel": "fast-demo", "programs": []}
    assert snapshot["schedule_scope"] == "published"


def test_native_ag_ui_resume_also_approves(harness: HarnessFactory) -> None:
    h = harness(publish_script(), [text("Published.")])
    pending = interrupt_id(run(h.run("publish plan-1")))

    run(h.run(resume=[{"interruptId": pending, "status": "resolved", "payload": {"approved": True}}]))

    assert [name for name, _ in h.gateway.calls] == ["schedule___publish"]


def declined(content: str) -> dict[str, Any]:
    envelope = json.loads(content)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "DECLINED"
    assert envelope["error"]["retryable"] is False
    return dict(envelope["error"])


def test_declined_call_is_cancelled_with_the_note(harness: HarnessFactory) -> None:
    h = harness(publish_script(), [text("Understood, not publishing.")])
    pending = interrupt_id(run(h.run("publish plan-1")))

    events = run(h.run(approval={"interrupt_id": pending, "approved": False, "note": "not now"}))

    assert h.gateway.calls == []
    content = of_kind(events, "TOOL_CALL_RESULT")[0]["content"]
    assert declined(content)["message"] == "The operator declined schedule___publish: not now"
    history = h.models[-1][1].calls[0]["messages"]
    model_result = next(b["toolResult"] for m in history for b in m["content"] if "toolResult" in b)
    assert model_result["content"] == [{"text": content}]  # the model reads the same envelope as the console
    assert "STATE_SNAPSHOT" not in kinds(events)


def test_new_message_declines_pending_approval(harness: HarnessFactory) -> None:
    h = harness(publish_script(), [text("OK, I will plan Sintel instead.")])
    run(h.run("publish plan-1"))

    events = run(h.run("no, use Sintel instead"))

    assert h.gateway.calls == []
    message = declined(of_kind(events, "TOOL_CALL_RESULT")[0]["content"])["message"]
    assert message == "The operator declined schedule___publish: no, use Sintel instead"
    assert events[-1]["type"] == "RUN_FINISHED"


def test_failed_tool_call_reaches_the_client_as_an_error_envelope(harness: HarnessFactory) -> None:
    def denied(_: dict[str, Any]) -> dict[str, Any]:
        raise PermissionError("Tool call denied by policy")

    h = harness(
        [tool_use("t1", "catalog___search_content", {"text": "bunny"})],
        [text("I am not allowed to search.")],
        handlers={"catalog___search_content": denied},
    )

    events = run(h.run("find bunny"))

    error = json.loads(of_kind(events, "TOOL_CALL_RESULT")[0]["content"])["error"]
    assert error["code"] == "TOOL_ERROR"
    assert "Tool call denied by policy" in error["message"]


def program(name: str, start: str) -> dict[str, Any]:
    return {"name": name, "title": name, "start": start, "end": start, "status": "PUBLISHED"}


def test_publish_is_folded_into_the_schedule_already_shown(harness: HarnessFactory) -> None:
    on_air = program("a", "2026-10-16T12:00:00.000Z")
    block = [program("b", "2026-10-16T12:30:00.000Z"), program("c", "2026-10-16T12:40:00.000Z")]
    handlers = {
        "schedule___get_schedule": lambda args: {
            "ok": True,
            "channel": args["channel"],
            "programs": [on_air],
            "gaps": [],
        },
        "schedule___publish": lambda args: {"ok": True, "channel": "fast-demo", "plan_id": "p", "programs": block},
    }
    h = harness(
        [tool_use("g1", "schedule___get_schedule", {"channel": "fast-demo"})],
        [text("Publishing.")],
        [tool_use("p1", "schedule___publish", {"plan_id": "p"})],
        [text("Published.")],
        handlers=handlers,
    )
    shown = of_kind(run(h.run("what is on fast-demo?")), "STATE_SNAPSHOT")[0]["snapshot"]
    pending = interrupt_id(run(h.run("publish p", state=shown)))

    events = run(h.run(approval={"interrupt_id": pending, "approved": True}, state=shown))

    snapshot = of_kind(events, "STATE_SNAPSHOT")[0]["snapshot"]
    assert shown["schedule"] == {"channel": "fast-demo", "programs": [on_air]}
    assert (snapshot["schedule_scope"], [p["name"] for p in snapshot["schedule"]["programs"]]) == (
        "channel",
        ["a", "b", "c"],
    )


def test_answered_approvals_cannot_be_replayed(harness: HarnessFactory, tmp_path: Path) -> None:
    h = harness(publish_script(), [text("Published.")])
    pending = interrupt_id(run(h.run("publish plan-1")))
    run(h.run(approval={"interrupt_id": pending, "approved": True}))

    stored = json.loads(next(tmp_path.rglob("agent.json")).read_text())
    events = run(h.run(approval={"interrupt_id": pending, "approved": True}))

    assert stored["state"][PENDING_APPROVALS_KEY] == {}
    assert events[-1] == {
        "type": "RUN_ERROR",
        "message": f"No pending approval with id {pending}.",
        "code": "APPROVAL_NOT_FOUND",
    }
    assert len(h.gateway.calls) == 1


def test_unknown_approval_is_rejected(harness: HarnessFactory) -> None:
    events = run(harness().run(approval={"interrupt_id": "v1:nope", "approved": True}))

    assert kinds(events) == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "APPROVAL_NOT_FOUND"


def test_missing_token_ends_the_run_with_run_error(harness: HarnessFactory) -> None:
    events = run(harness().run("hi", headers={}))

    assert kinds(events) == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "failure",
    [MCPClientInitializationError("boom"), AgentRunError("GATEWAY_UNAVAILABLE", "no tools for this caller")],
    ids=["unreachable", "no-tools"],
)
def test_gateway_failure_is_reported(settings: Settings, failure: Exception) -> None:
    @asynccontextmanager
    async def unavailable(_caller: Caller) -> AsyncIterator[list[Any]]:
        raise failure
        yield []

    scheduler = Scheduler(settings, tools=unavailable)
    run_input = RunAgentInput(thread_id="t", run_id="r", messages=[{"id": "1", "role": "user", "content": "hi"}])
    headers = {"Authorization": f"Bearer {unsigned_jwt(**OPERATOR_CLAIMS)}"}

    async def collect() -> list[Any]:
        return [e async for e in scheduler.run(run_input, session_id=None, headers=headers)]

    assert run(collect())[-1].code == "GATEWAY_UNAVAILABLE"


def test_machine_callers_and_plan_requests_use_plan_effort(harness: HarnessFactory) -> None:
    h = harness([text("ok")], [text("ok")], [text("ok")])

    run(h.run("extend fast-demo to 24 h", claims=AUTOPILOT_CLAIMS))
    run(h.run("hello"))
    run(h.run("build a block", mode="plan"))

    assert [mode for mode, _ in h.models] == [Mode.PLAN, Mode.CHAT, Mode.PLAN]


def test_context_is_sent_after_the_message(harness: HarnessFactory) -> None:
    h = harness([text("ok")])

    run(h.run("what is on?", context=[{"description": "Viewing channel", "value": "fast-demo"}]))

    user = h.models[0][1].calls[0]["messages"][-1]
    assert [b["text"] for b in user["content"]] == [
        "what is on?",
        "<console_context>\n- Viewing channel: fast-demo\n</console_context>",
    ]


def test_system_prompt_and_skills_reach_the_model(harness: HarnessFactory) -> None:
    h = harness([text("ok")])

    run(h.run("hi"))

    call = h.models[0][1].calls[0]
    assert "FAST Channels operator assistant" in call["system_prompt"]
    assert "<name>scheduling</name>" in call["system_prompt"]  # AgentSkills discloses the skill catalogue
    assert {spec["name"] for spec in call["tool_specs"]} >= {"skills", "schedule___plan", "schedule___publish"}
