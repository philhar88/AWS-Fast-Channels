"""The model-registry entry and its SSM name."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from fast_channels.core.model_registry import (
    RECOMMENDER_CHECKS,
    SCHEDULER_TASK,
    DataClass,
    Effort,
    EvalRecord,
    ModeEffort,
    ModelRoute,
    TaskEntry,
    TaskSettings,
    Thinking,
    model_family,
    qualification,
    record_key,
    record_path,
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


SCHEDULER = TaskSettings(model=HAIKU, eval="haiku-5-5-adaptive-16k")


def record(**overrides: object) -> EvalRecord:
    fields: dict[str, object] = {
        "task": SCHEDULER_TASK,
        "key": "haiku-5-5-adaptive-16k",
        "model": HAIKU,
        "effort": {"chat": "low", "plan": "medium"},
        "thinking": "adaptive",
        "max_tokens": 16_000,
        "suite": "scheduler conformance v1",
        "runs": 3,
        "passed": True,
        "checks": [
            {"name": "scenario_standard", "passed": True, "value": 1.0, "threshold": "3/3 runs"},
            {"name": "scenario_hard", "passed": True, "value": 1.0, "threshold": "3/3 runs"},
        ],
        "cost_usd": "0.0335",
        "recorded_at": "2026-10-29T01:36:17Z",
    }
    return EvalRecord.model_validate({**fields, **overrides})


@pytest.mark.parametrize(
    ("model", "family"),
    [
        (HAIKU, "anthropic.claude-haiku-5-5"),
        ("global.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"),
        ("{geo}.anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"),
        ("anthropic.claude-haiku-5-5", "anthropic.claude-haiku-5-5"),
    ],
)
def test_model_family_ignores_routing(model: str, family: str) -> None:
    assert model_family(model) == family


@pytest.mark.parametrize(
    ("model", "thinking", "max_tokens", "key"),
    [
        (HAIKU, Thinking.ADAPTIVE, 16_000, "haiku-5-5-adaptive-16k"),
        ("eu.anthropic.claude-sonnet-5-5", Thinking.DISABLED, 8_000, "sonnet-5-5-disabled-8k"),
        ("us.amazon.nova-2-lite-v1:0", Thinking.ADAPTIVE, 4_500, "amazon.nova-2-lite-v1-0-adaptive-4500"),
    ],
)
def test_record_keys(model: str, thinking: Thinking, max_tokens: int, key: str) -> None:
    assert record_key(model, thinking, max_tokens) == key


def test_a_passing_record_qualifies_every_routing_of_its_model() -> None:
    global_haiku = SCHEDULER.model_copy(update={"model": "global.anthropic.claude-haiku-5-5"})

    assert record().problems(SCHEDULER_TASK, SCHEDULER) == []
    assert record().problems(SCHEDULER_TASK, global_haiku) == []


def test_what_disqualifies_a_record() -> None:
    sonnet = SCHEDULER.model_copy(update={"model": "us.anthropic.claude-sonnet-5-5"})
    failed = record(
        passed=False,
        checks=[{"name": "scenario_standard", "passed": False}, {"name": "scenario_hard", "passed": True}],
    )

    assert record().problems("recommender", SCHEDULER)[0] == "it is a record of task 'scheduler.plan'"
    assert failed.problems(SCHEDULER_TASK, SCHEDULER) == ["it did not pass (scenario_standard)"]
    assert record().problems(SCHEDULER_TASK, sonnet) == [
        "it tested anthropic.claude-haiku-5-5, not anthropic.claude-sonnet-5-5"
    ]
    assert record(effort="low").problems(SCHEDULER_TASK, SCHEDULER) == [
        "it ran with effort low, thinking adaptive and max_tokens 16000, the entry has chat low / plan medium, "
        "adaptive and 16000"
    ]
    assert record(checks=[{"name": "scenario_hard", "passed": True}]).problems(SCHEDULER_TASK, SCHEDULER) == [
        "it lacks the checks ['scenario_standard'] that task scheduler.plan requires"
    ]


def test_a_record_cannot_claim_a_pass_its_checks_do_not_show() -> None:
    with pytest.raises(ValidationError, match="passed must be true exactly when every check passed"):
        record(passed=True, checks=[{"name": "scenario_standard", "passed": False}])


def test_recommender_records_need_the_faithfulness_check() -> None:
    settings = TaskSettings(model=HAIKU, effort=Effort.LOW, max_tokens=8000, eval="haiku-5-5-adaptive-8k")
    rec = record(
        task="recommender",
        key="haiku-5-5-adaptive-8k",
        effort="low",
        max_tokens=8000,
        checks=[{"name": "schema_valid", "passed": True}, {"name": "policy_violations", "passed": True}],
    )

    assert rec.problems("recommender", settings) == [
        "it lacks the checks ['faithfulness', 'cost_per_rail'] that task recommender requires"
    ]
    assert set(RECOMMENDER_CHECKS) == {"schema_valid", "faithfulness", "policy_violations", "cost_per_rail"}


def test_qualification_reads_the_first_directory_with_the_record(tmp_path: Path) -> None:
    empty, records = tmp_path / "empty", tmp_path / "records"
    path = record_path(records, SCHEDULER_TASK, "haiku-5-5-adaptive-16k")
    path.parent.mkdir(parents=True)
    path.write_text(record().model_dump_json())
    unnamed = SCHEDULER.model_copy(update={"eval": None})
    shorter = SCHEDULER.model_copy(update={"max_tokens": 8000})

    assert qualification((empty, records), SCHEDULER_TASK, SCHEDULER) == []
    assert qualification((empty,), SCHEDULER_TASK, SCHEDULER) == [
        f"eval record 'haiku-5-5-adaptive-16k' does not exist ({empty}/scheduler.plan/haiku-5-5-adaptive-16k.json); "
        "run make eval TASK=scheduler.plan MODEL=<model>"
    ]
    assert qualification((records,), SCHEDULER_TASK, unnamed) == [
        "names no eval record; run make eval TASK=scheduler.plan MODEL=<model> and set eval (e.g. "
        "'haiku-5-5-adaptive-16k')"
    ]
    [problem] = qualification((records,), SCHEDULER_TASK, shorter)
    assert problem.startswith("eval record 'haiku-5-5-adaptive-16k' does not qualify it: it ran with")


def test_qualification_refuses_an_unreadable_or_misnamed_record(tmp_path: Path) -> None:
    path = record_path(tmp_path, SCHEDULER_TASK, "haiku-5-5-adaptive-16k")
    path.parent.mkdir(parents=True)
    path.write_text('{"task": "scheduler.plan"}')
    [invalid] = qualification((tmp_path,), SCHEDULER_TASK, SCHEDULER)
    path.write_text(record(key="other").model_dump_json())
    [misnamed] = qualification((tmp_path,), SCHEDULER_TASK, SCHEDULER)

    assert invalid.startswith(f"eval record {path} is invalid:")
    assert misnamed == f"eval record {path} has key 'other'"
