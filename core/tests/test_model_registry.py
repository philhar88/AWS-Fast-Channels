"""The model-registry entry and its SSM name."""

import pytest
from pydantic import ValidationError

from fast_channels.core.model_registry import (
    SCHEDULER_TASK,
    DataClass,
    Effort,
    ModeEffort,
    ModelRoute,
    TaskEntry,
    Thinking,
    task_parameter,
)

HAIKU = "us.anthropic.claude-haiku-5-5"


def test_defaults_are_the_scheduler_agents_v3_model_spec() -> None:
    entry = TaskEntry(model=HAIKU)

    assert entry.effort == ModeEffort(chat=Effort.LOW, plan=Effort.MEDIUM)
    assert (entry.route, entry.thinking, entry.max_tokens) == (ModelRoute.BEDROCK, Thinking.ADAPTIVE, 16_000)
    assert (entry.data_class, entry.daily_cap_usd, entry.eval) == (DataClass.OPERATIONAL, None, None)


def test_an_entry_round_trips_through_its_json_document() -> None:
    entry = TaskEntry.model_validate(
        {"model": HAIKU, "effort": "low", "max_tokens": 8000, "data_class": "viewer", "daily_cap_usd": 30}
    )

    assert TaskEntry.model_validate_json(entry.model_dump_json()) == entry
    assert entry.model_dump(mode="json")["effort"] == "low"


@pytest.mark.parametrize(
    "fields",
    [
        {"model": HAIKU, "provider": "openai"},  # no endpoint or provider is data
        {"model": HAIKU, "route": "gateway"},  # the AgentCore Gateway route is not built
        {"model": HAIKU, "thinking": "enabled"},
        {"model": HAIKU, "max_tokens": 0},
        {"model": HAIKU, "daily_cap_usd": 0},
        {"model": HAIKU, "eval": "Haiku 5.5"},
        {"effort": "low"},
    ],
)
def test_invalid_entries_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskEntry.model_validate(fields)


def test_viewer_data_never_uses_a_global_profile() -> None:
    with pytest.raises(ValidationError, match="a task with viewer data needs a geographic profile"):
        TaskEntry(model="global.anthropic.claude-haiku-5-5", data_class=DataClass.VIEWER)

    assert TaskEntry(model="global.anthropic.claude-haiku-5-5").data_class is DataClass.OPERATIONAL
    assert TaskEntry(model=HAIKU, data_class=DataClass.VIEWER).model == HAIKU


def test_task_parameter() -> None:
    assert task_parameter("fastm", SCHEDULER_TASK) == "/fastm/models/tasks/scheduler.plan"
    assert task_parameter("fast", "recommender.cohort") == "/fast/models/tasks/recommender.cohort"


@pytest.mark.parametrize("task", ["Scheduler", "scheduler..plan", ".plan", "plan.", "rec/../x", ""])
def test_task_parameter_refuses_other_names(task: str) -> None:
    with pytest.raises(ValueError, match="dot-separated lowercase words"):
        task_parameter("fast", task)
