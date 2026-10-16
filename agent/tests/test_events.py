import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from fast_channels.agent.events import EventTranslator, result_text

if TYPE_CHECKING:
    from ag_ui.core import BaseEvent


def types(events: Iterable[BaseEvent]) -> list[str]:
    return [e.type.value for e in events]


def test_text_is_closed_by_a_tool_call_and_calls_by_the_message() -> None:
    snapshots = {"t1": {"channel": "c", "plan": None, "schedule": None}}
    translator = EventTranslator(lambda call_id: snapshots.pop(call_id, None))
    use = {"toolUseId": "t1", "name": "schedule___get_schedule"}
    stream: list[dict[str, Any]] = [
        {"data": "Let me"},
        {"data": ""},
        {"data": " check."},
        {"type": "tool_use_stream", "current_tool_use": {**use, "input": ""}, "delta": {"toolUse": {"input": ""}}},
        {"type": "tool_use_stream", "current_tool_use": {**use, "input": "{}"}, "delta": {"toolUse": {"input": "{}"}}},
        {"message": {"role": "assistant", "content": []}},
        {
            "message": {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "t1", "status": "success", "content": [{"json": {"ok": True}}]}}
                ],
            }
        },
        {"result": object()},
    ]

    events = [e for raw in stream for e in translator.translate(raw)]

    assert types(events) == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "TOOL_CALL_START",
        "TOOL_CALL_ARGS",
        "TOOL_CALL_END",
        "TOOL_CALL_RESULT",
        "STATE_SNAPSHOT",
    ]
    assert events[0].message_id == events[3].message_id  # type: ignore[attr-defined]
    assert events[7].content == '{"ok": true}'  # type: ignore[attr-defined]
    assert list(translator.close()) == []


def test_close_ends_whatever_is_open() -> None:
    translator = EventTranslator(lambda _: None)
    list(translator.translate({"current_tool_use": {"toolUseId": "t", "name": "x___y"}}))
    list(translator.translate({"data": "partial"}))

    assert types(translator.close()) == ["TEXT_MESSAGE_END", "TOOL_CALL_END"]


def test_result_text_joins_text_and_json_blocks() -> None:
    result: Any = {"toolUseId": "t", "status": "error", "content": [{"text": "a"}, {"json": [1]}, {"image": {}}]}

    assert result_text(result) == "a\n[1]"


def result_content_of(result: dict[str, Any]) -> str:
    translator = EventTranslator(lambda _: None)
    (event,) = translator.translate({"message": {"role": "user", "content": [{"toolResult": result}]}})
    content: str = event.content  # type: ignore[attr-defined]
    return content


def test_failed_calls_without_an_envelope_are_wrapped_as_tool_errors() -> None:
    # What Strands returns for a Cedar denial or a Gateway failure: plain text with status error.
    denied = {"toolUseId": "t", "status": "error", "content": [{"text": "Tool call denied by policy"}]}

    assert json.loads(result_content_of(denied)) == {
        "ok": False,
        "error": {"code": "TOOL_ERROR", "message": "Tool call denied by policy", "retryable": False, "hint": ""},
    }
    assert json.loads(result_content_of({**denied, "content": []}))["error"]["message"] == "The tool call failed."


def test_envelopes_and_successful_results_pass_through() -> None:
    declined = '{"ok": false, "error": {"code": "DECLINED", "message": "no"}}'

    assert result_content_of({"toolUseId": "t", "status": "error", "content": [{"text": declined}]}) == declined
    assert result_content_of({"toolUseId": "t", "status": "success", "content": [{"text": "Loaded skill."}]}) == (
        "Loaded skill."
    )
