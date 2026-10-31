"""Offline tests of ``scripts/eval_model.py --task`` (not marked e2e): the registry task's settings, the eval record
it writes, and the committed record that qualifies the shipped ``scheduler.plan`` entry."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from fast_channels.agent.evals.runner import RunRecord
from fast_channels.agent.model import Effort, ModelSpec
from fast_channels.core.model_registry import (
    SCHEDULER_TASK,
    EvalRecord,
    ModeEffort,
    TaskSettings,
    Thinking,
    qualification,
)

REPO = Path(__file__).resolve().parents[2]
HAIKU = "us.anthropic.claude-haiku-5-5"


def _eval_model() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fast_script_eval_model", REPO / "scripts" / "eval_model.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


eval_model = _eval_model()


def run(scenario: str, passed: bool, cost: float | None = 0.004) -> RunRecord:
    return RunRecord(
        scenario=scenario,
        model_id=HAIKU,
        effort="medium",
        run=1,
        start_ms=0,
        latency_s=1.0,
        agent_runs=1,
        usage={},
        cost_usd=cost,
        error=None,
        result={"pass": passed, "score": 1.0, "checks": {}, "tool_calls": 1},
        transcript=[],
    )


def test_a_task_runs_with_its_settings_in_cdk_json() -> None:
    settings = eval_model.task_settings(SCHEDULER_TASK, HAIKU)

    assert settings.model == HAIKU  # the model under test, not cdk.json's {geo} id
    assert (settings.effort, settings.thinking, settings.max_tokens) == (ModeEffort(), Thinking.ADAPTIVE, 16_000)
    assert eval_model.task_spec(settings) == ModelSpec(model_id=HAIKU, effort=Effort(chat="low", plan="medium"))


def test_one_effort_level_runs_both_modes() -> None:
    settings = TaskSettings(model=HAIKU, effort="low", thinking="disabled", max_tokens=8000)

    assert eval_model.task_spec(settings) == ModelSpec(
        model_id=HAIKU, effort=Effort(chat="low", plan="low"), thinking="disabled", max_tokens=8000
    )


def test_an_unknown_task_is_refused() -> None:
    with pytest.raises(SystemExit, match=r"cdk\.json has no model-registry task 'recommender'"):
        eval_model.task_settings("recommender", HAIKU)


def test_the_record_has_a_check_per_scenario_and_fails_with_any_run(tmp_path: Path) -> None:
    settings = eval_model.task_settings(SCHEDULER_TASK, HAIKU)
    runs = [run("standard", True), run("standard", True), run("hard", True), run("hard", False, None)]

    record = eval_model.task_record(SCHEDULER_TASK, settings, 2, runs)
    path = eval_model.write_record(record, tmp_path)

    assert path == tmp_path / SCHEDULER_TASK / "haiku-5-5-adaptive-16k.json"
    assert EvalRecord.model_validate_json(path.read_text()) == record
    assert [(c.name, c.passed, c.value, c.threshold) for c in record.checks] == [
        ("scenario_hard", False, 0.5, "2/2 runs"),
        ("scenario_standard", True, 1.0, "2/2 runs"),
    ]
    assert (record.passed, str(record.cost_usd), record.runs) == (False, "0.012000", 2)
    assert qualification((tmp_path,), SCHEDULER_TASK, settings.model_copy(update={"eval": record.key})) == [
        "eval record 'haiku-5-5-adaptive-16k' does not qualify it: it did not pass (scenario_hard)"
    ]


def test_the_committed_record_qualifies_the_shipped_scheduler_entry() -> None:
    shipped = json.loads((REPO / "cdk.json").read_text())["context"]["fast-channels"]["modules"]["models"]["tasks"]
    entry = TaskSettings.model_validate(shipped[SCHEDULER_TASK])

    assert qualification((REPO / "evals" / "records",), SCHEDULER_TASK, entry) == []
