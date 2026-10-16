import asyncio
import itertools
import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core import RunAgentInput
from strands.session import FileSessionManager

from fast_channels.agent.evals.catalog import by_asset
from fast_channels.agent.evals.fake_tools import CHANNEL, TOOLS, FakeScheduleTools, gateway_definitions, iso, serve
from fast_channels.agent.evals.runner import RunRecord, rescore, save, summarise
from fast_channels.agent.evals.scenarios import HOUR_MS, SCENARIOS
from fast_channels.agent.evals.scoring import score, tool_calls
from fast_channels.agent.scheduler import Scheduler
from fast_channels.agent.settings import Settings
from fast_channels.agent.testing import OPERATOR_CLAIMS, ScriptedModel, kinds, text, tool_use, unsigned_jwt

START = (int(time.time() * 1000) // HOUR_MS + 2) * HOUR_MS  # a full hour, beyond the lead-time warning


def test_plan_computes_contiguous_on_air_durations_and_breaks() -> None:
    tools = FakeScheduleTools()

    plan = tools.plan(channel=CHANNEL, picks=["ff-002-a1b2c3", "ff-004-a1b2c3"], start=iso(START))

    first, second = plan["programs"]
    assert first["start_ms"] == START
    assert second["start_ms"] == START + first["duration_ms"]
    assert plan["end_ms"] == second["start_ms"] + second["duration_ms"]
    title = by_asset()["ff-002-a1b2c3"]
    assert first["duration_ms"] == title.duration_ms + 60_060 * len(first["breaks"])  # on air: content plus breaks
    offsets = [b["offset_ms"] for b in first["breaks"]]
    assert set(offsets) <= set(title.break_offsets_ms)
    assert all(b - a >= 8 * 60_000 for a, b in itertools.pairwise(offsets))
    assert (plan["violations"], plan["publishable"]) == ([], True)
    assert plan["plan_id"].startswith(f"{CHANNEL}.")


def test_plan_follows_a_named_program_and_refuses_ambiguous_anchors() -> None:
    tools = FakeScheduleTools()
    first = tools.plan(channel=CHANNEL, picks=["ff-004-a1b2c3"], start=iso(START))
    tools.publish(first["plan_id"])
    (program,) = first["programs"]

    after = tools.plan(channel=CHANNEL, picks=["ff-012-a1b2c3"], after=program["name"])

    assert after["start_ms"] == first["end_ms"]
    assert tools.plan(channel=CHANNEL, picks=["ff-012-a1b2c3"], after="nope")["error"]["code"] == "NOT_FOUND"
    both = tools.plan(channel=CHANNEL, picks=["ff-012-a1b2c3"], start=iso(START), after=program["name"])
    assert both["error"]["code"] == "VALIDATION"


def test_publishing_after_the_schedule_changed_is_stale() -> None:
    tools = FakeScheduleTools()
    first = tools.plan(channel=CHANNEL, picks=["ff-004-a1b2c3"], start=iso(START))
    second = tools.plan(channel=CHANNEL, picks=["ff-012-a1b2c3"], start=iso(START + HOUR_MS))
    tools.publish(first["plan_id"])

    assert tools.publish(second["plan_id"])["error"]["code"] == "PLAN_STALE"


def test_plan_flags_unknown_assets_and_ratings_and_publish_refuses_them() -> None:
    tools = FakeScheduleTools()

    plan = tools.plan(channel=CHANNEL, picks=["nope", "ff-003-a1b2c3"], start=iso(START))

    assert [(v["code"], v["severity"]) for v in plan["violations"]] == [("UNKNOWN_ASSET", "error"), ("RATING", "error")]
    assert tools.publish(plan["plan_id"])["error"]["code"] == "VALIDATION"
    assert tools.publish("plan-9999")["error"]["code"] == "NOT_FOUND"
    assert tools.published == []


def test_publish_appends_and_the_next_plan_follows() -> None:
    tools = FakeScheduleTools()
    first = tools.plan(channel=CHANNEL, picks=["ff-004-a1b2c3"], start=iso(START))
    tools.publish(first["plan_id"])

    follow_up = tools.plan(channel=CHANNEL, picks=["ff-012-a1b2c3"])

    assert follow_up["start_ms"] == first["end_ms"]
    assert tools.get_schedule(CHANNEL)["end_ms"] == first["end_ms"]


@pytest.mark.parametrize(
    ("name", "lineup"),
    [
        ("standard", ["ff-009", "ff-002", "ff-004", "ff-012"]),
        # Found by brute force over the fake tools' arithmetic: the hard block is feasible but tight.
        ("hard", ["ff-001", "ff-002", "ff-004", "ff-006", "ff-008", "ff-011", "ff-009", "ff-012"]),
    ],
)
def test_every_scenario_has_a_passing_lineup(name: str, lineup: list[str]) -> None:
    result = score(_transcript([f"{content_id}-a1b2c3" for content_id in lineup]), SCENARIOS[name], START)

    assert result["pass"], result["checks"]


def _transcript(picks: list[str]) -> list[dict[str, Any]]:
    tools = FakeScheduleTools()
    plan = tools.plan(channel=CHANNEL, picks=picks, start=iso(START))
    published = tools.publish(plan["plan_id"])
    return [
        *_call("p", "schedule___plan", {"picks": picks}, plan),
        {"type": "CUSTOM", "name": "approval_required", "value": {}},
        *_call("q", "schedule___publish", {"plan_id": plan["plan_id"]}, published),
    ]


def _call(call_id: str, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "TOOL_CALL_START", "toolCallId": call_id, "toolCallName": name},
        {"type": "TOOL_CALL_ARGS", "toolCallId": call_id, "delta": json.dumps(arguments)},
        {"type": "TOOL_CALL_RESULT", "toolCallId": call_id, "content": json.dumps(result)},
    ]


def test_the_fake_server_advertises_the_gateway_definitions() -> None:
    advertised = asyncio.run(FakeScheduleTools().server().list_tools())

    definitions = gateway_definitions()
    assert [t.name for t in advertised] == list(TOOLS)
    for tool in advertised:
        assert (tool.description, tool.input_schema) == (
            definitions[tool.name]["description"],
            definitions[tool.name]["inputSchema"],
        )


def test_score_fails_unpublished_and_rule_breaking_runs() -> None:
    unpublished = _transcript(["ff-004-a1b2c3"])[:3]
    overrun = _transcript(["ff-009-a1b2c3", "ff-011-a1b2c3", "ff-002-a1b2c3"])

    assert score(unpublished, SCENARIOS["standard"], START)["checks"]["published"] is False
    checks = score(overrun, SCENARIOS["standard"], START)["checks"]
    assert checks["within_block"] is False
    assert checks["published"] and checks["family_only"] and checks["approval_requested"]


def test_score_reads_live_plans_without_inline_metadata() -> None:
    plan = {
        "plan_id": "p1",
        "start_ms": START,
        "end_ms": START + 3 * HOUR_MS,
        "violations": [],
        "programs": [{"content_id": "ff-003", "start_ms": START}],
    }
    transcript = [
        *_call(
            "c",
            "catalog___search_content",
            {},
            {
                "ok": True,
                "items": [
                    {"content_id": "ff-003", "title": "Midnight Heist", "rating": "TV-MA", "genres": ["Thriller"]}
                ],
            },
        ),
        *_call("p", "schedule___plan", {}, {"ok": True, **plan}),
        *_call("q", "schedule___publish", {"plan_id": "p1"}, {"ok": True, "plan_id": "p1"}),
    ]

    assert score(transcript, SCENARIOS["standard"], START)["checks"]["family_only"] is False


def test_tool_calls_reassemble_streamed_arguments() -> None:
    events = [
        {"type": "TOOL_CALL_START", "toolCallId": "t", "toolCallName": "schedule___plan"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t", "delta": '{"picks": '},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t", "delta": '["a"]}'},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "t", "content": "The operator declined schedule___plan."},
    ]

    (call,) = tool_calls(events)

    assert call.arguments == {"picks": ["a"]}
    assert call.result is None and not call.ok


def test_scheduler_runs_over_the_fake_mcp_server(tmp_path: Path) -> None:
    """The real Gateway client path (MCP streamable HTTP) with a scripted model, approval included."""
    picks = ["ff-002-a1b2c3", "ff-004-a1b2c3"]
    turns = [
        [tool_use("p", "schedule___plan", {"channel": CHANNEL, "picks": picks, "start": iso(START)})],
        [tool_use("q", "schedule___publish", {"plan_id": f"{CHANNEL}.00000001"})],
        [text("Published.")],
    ]
    tools = FakeScheduleTools()
    headers = {"Authorization": f"Bearer {unsigned_jwt(**OPERATOR_CLAIMS)}"}

    async def converse(url: str) -> list[dict[str, Any]]:
        scheduler = Scheduler(
            Settings(region="us-east-1", gateway_url=url, confirm_tools=frozenset({"schedule___publish"})),
            model=lambda _spec, _mode: ScriptedModel(turns),
            sessions=lambda sid, _caller: FileSessionManager(session_id=sid, storage_dir=str(tmp_path)),
        )
        events: list[dict[str, Any]] = []
        props: dict[str, Any] = {}
        message: str | None = "fill the block"
        for _ in range(2):
            run_input = RunAgentInput(
                thread_id="eval-thread-0123456789abcdef0123456789",
                run_id=str(uuid.uuid4()),
                forwarded_props=props,
                messages=[{"id": "m", "role": "user", "content": message}] if message else [],
            )  # type: ignore[list-item]
            batch = [
                e.model_dump(mode="json", by_alias=True)
                async for e in scheduler.run(run_input, session_id=run_input.thread_id, headers=headers)
            ]
            events += batch
            approvals = [e["value"]["interrupt_id"] for e in batch if e["type"] == "CUSTOM"]
            message, props = None, {"approval": {"interrupt_id": approvals[0], "approved": True}} if approvals else {}
        return events

    with serve(tools) as url:
        events = asyncio.run(converse(url))

    assert kinds(events).count("RUN_FINISHED") == 2
    assert "RUN_ERROR" not in kinds(events)
    assert [p["asset_id"] for p in tools.published] == picks
    assert score(events, SCENARIOS["standard"], START)["checks"]["approval_requested"]


def test_rescore_round_trip(tmp_path: Path) -> None:
    transcript = _transcript(["ff-004-a1b2c3"])
    record = RunRecord(
        scenario="standard",
        model_id="m",
        effort="medium",
        run=1,
        start_ms=START,
        latency_s=1.0,
        agent_runs=2,
        usage={"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
        cost_usd=None,
        error=None,
        result=score([], SCENARIOS["standard"], START),
        transcript=transcript,
    )

    (rescored,) = rescore([save([record], tmp_path)])

    assert rescored.result == score(transcript, SCENARIOS["standard"], START)
    assert "| m | medium | standard | 1 | 0/1 |" in summarise([rescored])
