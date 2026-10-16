"""Scoring of a run from its AG-UI transcript (works for the fake tools and for a live Gateway alike).

A run passes when every check holds. The checks are the benchmark's (models-ui.md §2.1) restated for v3 tools:
the model has to read the brief, pick valid titles, plan, read the result and publish what it planned.
"""

import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fast_channels.agent.evals.catalog import FAMILY_RATINGS
from fast_channels.agent.evals.scenarios import HOUR_MS, Scenario
from fast_channels.agent.gateway import is_gateway_tool

type Event = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool call reconstructed from TOOL_CALL_START/ARGS/RESULT events."""

    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return bool(self.result and self.result.get("ok", True) is not False)


def tool_calls(transcript: Iterable[Event]) -> list[ToolCall]:
    """Tool calls in order of their start, with parsed arguments and JSON results."""
    names: dict[str, str] = {}
    arguments: dict[str, str] = {}
    results: dict[str, dict[str, Any] | None] = {}
    for event in transcript:
        match event:
            case {"type": "TOOL_CALL_START", "toolCallId": call_id, "toolCallName": name}:
                names[call_id], arguments[call_id] = name, ""
            case {"type": "TOOL_CALL_ARGS", "toolCallId": call_id, "delta": delta}:
                arguments[call_id] = arguments.get(call_id, "") + delta
            case {"type": "TOOL_CALL_RESULT", "toolCallId": call_id, "content": content}:
                results[call_id] = _json_object(content)
    return [
        ToolCall(name, _json_object(arguments[call_id]) or {}, results.get(call_id)) for call_id, name in names.items()
    ]


def score(transcript: Sequence[Event], scenario: Scenario, start_ms: int) -> dict[str, Any]:
    """Checks for one run: ``{"checks": {...}, "score": fraction passed, "pass": all passed, ...}``."""
    calls = tool_calls(transcript)
    plans = {c.result["plan_id"]: c.result for c in calls if c.name == "schedule___plan" and c.ok and c.result}
    published_ids = [
        c.result.get("plan_id") or c.arguments.get("plan_id")
        for c in calls
        if c.name == "schedule___publish" and c.ok and c.result
    ]
    plan = next((plans[plan_id] for plan_id in reversed(published_ids) if plan_id in plans), None)
    programs = _with_metadata(plan["programs"] if plan else [], calls)
    end_ms = start_ms + scenario.hours * HOUR_MS
    titles = [p.get("title") or p.get("content_id") for p in programs]
    checks = {
        "published": plan is not None,
        "approval_requested": any(
            e.get("type") == "CUSTOM" and e.get("name") == "approval_required" for e in transcript
        ),
        "plan_valid": plan is not None and not any(v["severity"] == "error" for v in plan["violations"]),
        "family_only": bool(programs) and all(p.get("rating") in FAMILY_RATINGS for p in programs),
        "no_repeats": len(set(titles)) == len(titles),
        "starts_on_time": plan is not None and plan["start_ms"] == start_ms,
        "within_block": plan is not None and plan["end_ms"] <= end_ms,
        "fill_95": plan is not None and plan["end_ms"] - plan["start_ms"] >= 0.95 * scenario.hours * HOUR_MS,
    }
    if scenario.alternate_genres:
        checks["genre_alternates"] = bool(programs) and all(
            a["genres"] != b["genres"] for a, b in itertools.pairwise(programs)
        )
    if scenario.prime_time_title:
        movie = next((p for p in programs if p.get("title") == scenario.prime_time_title), None)
        checks["prime_time_title"] = movie is not None and movie["start_ms"] >= start_ms + 3 * HOUR_MS
    return {
        "checks": checks,
        "score": round(sum(checks.values()) / len(checks), 3),
        "pass": all(checks.values()),
        "lineup": titles,
        "tool_calls": len(calls),
        "tool_errors": sum(not c.ok for c in calls if is_gateway_tool(c.name)),
    }


def _with_metadata(programs: list[dict[str, Any]], calls: list[ToolCall]) -> list[dict[str, Any]]:
    """Fill title/rating/genres from catalog results seen in the run (live plans carry only content ids)."""
    catalog = {
        item["content_id"]: item
        for c in calls
        if c.name.startswith("catalog___") and c.result
        for item in c.result.get("items", [c.result])
        if isinstance(item, dict) and "content_id" in item
    }
    return [{**catalog.get(p.get("content_id"), {}), **p} for p in programs]


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
