import json
from types import SimpleNamespace
from typing import Any

from fast_channels.agent.hooks import SharedStateTracker, fold_guide
from fast_channels.agent.protocol import ScheduleScope, SharedState


def program(name: str, minute: int) -> dict[str, Any]:
    return {"name": name, "start": f"2026-10-16T12:{minute:02d}:00.000Z", "status": "PUBLISHED"}


def call(tracker: SharedStateTracker, tool: str, result: dict[str, Any], call_id: str = "t") -> dict[str, Any] | None:
    """Run the tracker's AfterToolCall hook for one Gateway result and take its snapshot."""
    event = SimpleNamespace(
        tool_use={"toolUseId": call_id, "name": tool, "input": {}},
        result={"toolUseId": call_id, "status": "success", "content": [{"text": json.dumps(result)}]},
    )
    tracker._after_tool_call(event)  # type: ignore[arg-type]
    return tracker.take(call_id)


GUIDE = {"ok": True, "channel": "fast-demo", "generated_at": "g1", "ladder_version": "v1", "gaps": []}


def test_fold_guide_upserts_removes_and_keeps_air_order() -> None:
    guide = {"channel": "fast-demo", "generated_at": "g1", "programs": [program("a", 0), program("b", 10)]}
    change = {"channel": "fast-demo", "generated_at": "g2", "plan_id": "p"}

    folded = fold_guide(guide, change, upserted=[program("c", 5), {**program("b", 20), "status": "AIRED"}])

    assert folded == {
        "channel": "fast-demo",
        "generated_at": "g2",
        "programs": [program("a", 0), program("c", 5), {**program("b", 20), "status": "AIRED"}],
    }
    assert [p["name"] for p in fold_guide(folded, {}, removed=["c"])["programs"]] == ["a", "b"]


def test_fold_guide_drops_what_is_not_a_program_from_a_client_echoed_guide() -> None:
    assert fold_guide({"programs": [1, None, program("a", 0)]}, {})["programs"] == [program("a", 0)]
    assert fold_guide({"programs": "junk"}, {}, upserted=[program("b", 1)])["programs"] == [program("b", 1)]


def test_publish_and_removal_change_the_guide_shown_rather_than_replace_it() -> None:
    tracker = SharedStateTracker(SharedState())

    shown = call(tracker, "schedule___get_schedule", {**GUIDE, "programs": [program("a", 0)]})
    published = call(tracker, "schedule___publish", {**GUIDE, "programs": [program("b", 10), program("c", 20)]})
    removed = call(
        tracker, "schedule___remove_programs", {"channel": "fast-demo", "removed": ["b"], "moved": [program("c", 10)]}
    )

    assert shown == {
        "channel": "fast-demo",
        "plan": None,
        "schedule": {
            "channel": "fast-demo",
            "generated_at": "g1",
            "ladder_version": "v1",
            "programs": [program("a", 0)],
        },
        "schedule_scope": "channel",
    }
    assert published is not None and removed is not None
    assert [p["name"] for p in published["schedule"]["programs"]] == ["a", "b", "c"]
    assert published["schedule_scope"] == "channel"
    assert removed["schedule"]["programs"] == [program("a", 0), program("c", 10)]


def test_publish_without_a_guide_is_labelled_as_published_programs_only() -> None:
    tracker = SharedStateTracker(SharedState(channel="fast-demo", plan={"plan_id": "p"}))

    first = call(tracker, "schedule___publish", {**GUIDE, "programs": [program("a", 0)]})
    second = call(tracker, "schedule___publish", {**GUIDE, "programs": [program("b", 10)]})

    assert first is not None and second is not None
    assert (first["plan"], first["schedule_scope"]) == (None, ScheduleScope.PUBLISHED)
    assert [p["name"] for p in second["schedule"]["programs"]] == ["a", "b"]
    assert second["schedule_scope"] == ScheduleScope.PUBLISHED


def test_another_channel_starts_a_fresh_state_and_removal_needs_a_guide() -> None:
    tracker = SharedStateTracker(SharedState())
    call(tracker, "schedule___get_schedule", {**GUIDE, "programs": [program("a", 0)]})

    other = call(tracker, "schedule___publish", {**GUIDE, "channel": "fast-kids", "programs": [program("k", 0)]})
    removal = {"channel": "fast-news", "removed": ["n"], "moved": []}

    assert other is not None
    assert (other["channel"], other["schedule_scope"]) == ("fast-kids", "published")
    assert call(tracker, "schedule___remove_programs", removal) is None
    assert call(tracker, "schedule___publish", {"ok": False, "error": {"code": "PLAN_STALE"}}) is None
