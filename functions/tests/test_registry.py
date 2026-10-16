import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import BaseModel, Field

from fast_channels.functions.tools import dispatch, registry
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, gateway_tool_schema


class Pick(BaseModel):
    asset_id: str = Field(description="Library asset id")
    breaks: int = Field(default=2, ge=0, le=6)


class Policy(BaseModel):
    """A model the tools reuse but do not own (like the ``fast_channels.core`` ones)."""

    cadence: float = Field(12, gt=0)
    slate: int = Field(30, description="Own description.")
    tags: list[str] = []


class PlanInput(BaseModel):
    channel: str = Field(description="Channel name (channels___list_channels)", max_length=64)
    mode: Literal["append", "replace"] = "append"
    picks: list[Pick]
    note: str | None = None
    policy: Policy = Policy()
    override: Policy | None = Field(None, description="Overrides the policy.")


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

    def walk(node: dict[str, Any]) -> None:
        assert set(node) <= allowed, node
        for child in node.get("properties", {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    walk(schema)
    properties = schema["properties"]
    assert schema["required"] == ["channel", "picks"]
    assert properties["mode"]["description"] == "one of append, replace; default append"
    assert properties["picks"]["items"]["properties"]["breaks"]["description"] == "default 2; ≥ 0; ≤ 6"
    assert properties["note"] == {"type": "string"}  # no "default None", no title
    assert "correlation_id" in properties


def test_gateway_schema_merges_constraints_into_a_trailing_parenthesis(fake_target: str) -> None:
    schema = gateway_tool_schema(registry._REGISTRY[fake_target]["plan"])["inputSchema"]
    assert schema["properties"]["channel"]["description"] == "Channel name (channels___list_channels; max length 64)"


def test_described_fields_document_reused_models(fake_target: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_FIELD_DESCRIPTIONS", {})
    registry.describe_fields(Policy, cadence="Minutes between ``breaks``.", slate="Ignored: the field has one.")

    properties = gateway_tool_schema(registry._REGISTRY[fake_target]["plan"])["inputSchema"]["properties"]

    policy, override = properties["policy"], properties["override"]
    assert policy["description"] == (Policy.__doc__ or "").replace("``", "`")  # no folded object default
    assert policy["properties"]["cadence"]["description"] == "Minutes between `breaks` (default 12; > 0)."
    assert policy["properties"]["slate"]["description"] == "Own description (default 30)."
    assert policy["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}  # empty default dropped
    assert override["description"] == "Overrides the policy."
    assert override["properties"] == policy["properties"]


def test_describe_fields_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="no fields"):
        registry.describe_fields(Policy, cadense="typo")


def test_confirm_tools_tell_the_model_that_they_wait_for_approval(fake_target: str) -> None:
    schema = gateway_tool_schema(registry._REGISTRY[fake_target]["plan"])
    assert schema["description"] == f"Plan a block of programs. {registry.APPROVAL_NOTE}"


def _descriptions(node: dict[str, Any]) -> Iterator[tuple[str, str]]:
    for name, child in node.get("properties", {}).items():
        yield name, child.get("description", "")
        yield from _descriptions(child)
        yield from _descriptions(child.get("items", {}))


@pytest.mark.parametrize("spec", list(registry.all_tools()), ids=lambda spec: spec.qualified_name)
def test_every_registry_input_is_documented_for_the_model(spec: registry.ToolSpec[Any, Any]) -> None:
    schema = gateway_tool_schema(spec)
    assert "\n" not in schema["description"]
    for name, description in _descriptions(schema["inputSchema"]):
        assert description, f"{spec.qualified_name}: {name} has no description"
        assert not re.search(r"default (None|;|\)|$)|``|\) \(", description), f"{name}: {description}"
    assert (registry.APPROVAL_NOTE in schema["description"]) is (spec.approval is Approval.CONFIRM)


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
