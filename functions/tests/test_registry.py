from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from fast_channels.functions.tools import dispatch, registry
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, gateway_tool_schema


class Pick(BaseModel):
    asset_id: str = Field(description="Library asset id")
    breaks: int = Field(default=2, ge=0, le=6)


class PlanInput(BaseModel):
    channel: str = Field(description="Channel name", max_length=64)
    mode: Literal["append", "replace"] = "append"
    picks: list[Pick]
    note: str | None = None


class PlanOutput(BaseModel):
    plan_id: str
    programs: int


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setitem(registry._REGISTRY, "schedule", {})

    @registry.tool("schedule", access=Access.WRITE, approval=Approval.CONFIRM)
    def plan(payload: PlanInput, ctx: ToolContext) -> PlanOutput:
        """Plan a block of programs."""
        if payload.channel == "missing":
            raise ToolError("NOT_FOUND", "no such channel", hint="List channels first.")
        return PlanOutput(plan_id=f"p-{ctx.correlation_id}", programs=len(payload.picks))

    monkeypatch.setattr(dispatch, "load", lambda target: registry._REGISTRY[target])
    return "schedule"


def test_registration_captures_types_and_policy(fake_target: str) -> None:
    spec = registry._REGISTRY[fake_target]["plan"]
    assert spec.qualified_name == "schedule___plan"
    assert spec.input is PlanInput and spec.output is PlanOutput
    assert spec.approval is Approval.CONFIRM and spec.access is Access.WRITE
    assert spec.description == "Plan a block of programs."


def test_gateway_schema_is_flat_and_folds_constraints(fake_target: str) -> None:
    schema = gateway_tool_schema(registry._REGISTRY[fake_target]["plan"])["inputSchema"]
    allowed = {"type", "properties", "required", "items", "description"}

    def walk(node: dict) -> None:
        assert set(node) <= allowed, node
        for child in node.get("properties", {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    walk(schema)
    assert schema["required"] == ["channel", "picks"]
    assert "one of append, replace" in schema["properties"]["mode"]["description"]
    assert schema["properties"]["picks"]["items"]["properties"]["breaks"]["description"].endswith("≤ 6")
    assert schema["properties"]["note"]["type"] == "string"
    assert "correlation_id" in schema["properties"]


def test_duplicate_and_undocumented_tools_are_rejected(fake_target: str) -> None:
    with pytest.raises(ValueError, match="duplicate"):

        @registry.tool("schedule", access=Access.READ)
        def plan(payload: PlanInput, ctx: ToolContext) -> PlanOutput:
            """Again."""
            raise NotImplementedError

    with pytest.raises(ValueError, match="docstring"):

        @registry.tool("schedule", access=Access.READ)
        def silent(payload: PlanInput, ctx: ToolContext) -> PlanOutput:
            raise NotImplementedError


def _context(tool: str) -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool}),
        aws_request_id="req-1",
        function_name="f",
        function_version="1",
        invoked_function_arn="arn",
        memory_limit_in_mb=512,
        log_group_name="g",
        log_stream_name="s",
        get_remaining_time_in_millis=lambda: 1000,
    )


def test_dispatch_success_validation_and_tool_errors(fake_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    handle = dispatch.handler(fake_target)
    ok = handle({"channel": "c", "picks": [{"asset_id": "a"}], "correlation_id": "s1"}, _context("schedule___plan"))
    assert ok == {"ok": True, "plan_id": "p-s1", "programs": 1}

    invalid = handle({"picks": []}, _context("schedule___plan"))
    assert invalid["ok"] is False and invalid["error"]["code"] == "VALIDATION"

    missing = handle({"channel": "missing", "picks": []}, _context("schedule___plan"))
    assert missing["error"] == {
        "code": "NOT_FOUND",
        "message": "no such channel",
        "retryable": False,
        "hint": "List channels first.",
    }

    unknown = handle({}, _context("schedule___nope"))
    assert unknown["error"]["code"] == "NOT_FOUND"
