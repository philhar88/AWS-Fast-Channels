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
    TaskSettings,
    Thinking,
    task_parameter,
)

HAIKU = "us.anthropic.claude-haiku-5-5"
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123def456"


def test_defaults_are_the_scheduler_agents_v3_model_spec() -> None:
    settings = TaskSettings(model=HAIKU)

    assert settings.effort == ModeEffort(chat=Effort.LOW, plan=Effort.MEDIUM)
    assert (settings.route, settings.thinking, settings.max_tokens) == (ModelRoute.BEDROCK, Thinking.ADAPTIVE, 16_000)
    assert (settings.data_class, settings.daily_cap_usd, settings.eval) == (DataClass.OPERATIONAL, None, None)


def test_an_entry_is_the_settings_and_the_tasks_profile() -> None:
    settings = TaskSettings.model_validate(
        {"model": HAIKU, "effort": "low", "max_tokens": 8000, "data_class": "viewer", "daily_cap_usd": 30}
    )
    entry = TaskEntry.model_validate({**settings.model_dump(mode="json"), "profile_arn": PROFILE})

    assert TaskEntry.model_validate_json(entry.model_dump_json()) == entry
    assert entry.model_dump(mode="json") == {**settings.model_dump(mode="json"), "profile_arn": PROFILE}
    assert "profile_arn" not in TaskSettings.model_fields  # rendered by the deployment, never configured


BASE = {"model": HAIKU, "profile_arn": PROFILE}


@pytest.mark.parametrize(
    "fields",
    [
        {**BASE, "provider": "openai"},  # no endpoint or provider is data
        {**BASE, "route": "gateway"},  # the AgentCore Gateway route is not built
        {**BASE, "thinking": "enabled"},
        {**BASE, "max_tokens": 0},
        {**BASE, "daily_cap_usd": 0},
        {**BASE, "eval": "Haiku 5.5"},
        {"effort": "low", "profile_arn": PROFILE},  # no model
        {"model": HAIKU},  # no profile
        {**BASE, "profile_arn": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.x"},
    ],
)
def test_invalid_entries_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskEntry.model_validate(fields)


def test_viewer_data_never_uses_a_global_profile() -> None:
    with pytest.raises(ValidationError, match="a task with viewer data needs a geographic profile"):
        TaskSettings(model="global.anthropic.claude-haiku-5-5", data_class=DataClass.VIEWER)

    assert TaskSettings(model="global.anthropic.claude-haiku-5-5").data_class is DataClass.OPERATIONAL
    assert TaskSettings(model=HAIKU, data_class=DataClass.VIEWER).model == HAIKU


def test_task_parameter() -> None:
    assert task_parameter("fastm", SCHEDULER_TASK) == "/fastm/models/tasks/scheduler.plan"
    assert task_parameter("fast", "recommender.cohort") == "/fast/models/tasks/recommender.cohort"


@pytest.mark.parametrize("task", ["Scheduler", "scheduler..plan", ".plan", "plan.", "rec/../x", ""])
def test_task_parameter_refuses_other_names(task: str) -> None:
    with pytest.raises(ValueError, match="dot-separated lowercase words"):
        task_parameter("fast", task)
