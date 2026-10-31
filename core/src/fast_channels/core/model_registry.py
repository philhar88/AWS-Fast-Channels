"""The per-task model registry: one validated entry per LLM task, stored as SSM ``/<prefix>/models/tasks/<task>``.

Each task (``scheduler.plan`` for the scheduling agent; Phase-2 tasks such as ``recommender``) names its Bedrock
model, how it reaches it, and the request settings that stay fixed for the task, and the deployment gives it its own
application inference profile: the per-task cost and metrics primitive, and the only target the task's role may
invoke. Model ids are chosen only among the deployment's IAM allow-list (``agent.allowed_models``): the registry is
data, the allow-list is the security property, so a registry edit can only choose among models the deployment already
grants. Readers validate every entry and refuse one outside the allow-list.

``thinking`` and the output schema of a task never change per call: either change rewrites the prompt cache (a
per-call schema tripled the cost per call in the design measurements).

A model qualifies for a task only with a passing **eval record** (:class:`EvalRecord`,
``evals/records/<task>/<key>.json``): the task's conformance suite run on that model with the task's settings.
"""

import re
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEDULER_TASK: Final = "scheduler.plan"
"""The scheduling agent's task (the AgentCore Runtime in the operator plane)."""
TASK_NAME: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
"""Task names: dot-separated lowercase words (``scheduler.plan``, ``recommender.cohort``)."""
PROFILE_ARN: Final = r"^arn:aws[a-z-]*:bedrock:[a-z0-9-]+:\d{12}:application-inference-profile/[a-z0-9]+$"
"""An application inference profile's ARN."""
GLOBAL_PROFILE_PREFIX: Final = "global."
"""Global cross-Region inference profiles route a request to any commercial Region in the world."""
EVAL_KEY: Final = r"^[a-z0-9][a-z0-9.-]*$"
"""Eval record keys (file names without ``.json``): ``haiku-5-5-adaptive-16k``."""
_PROFILE_PREFIXES: Final = frozenset({"global", "us", "eu", "apac", "ca", "jp", "au", "us-gov", "{geo}"})
"""Inference-profile prefixes of model ids (``{geo}`` stands for the region's geography in configuration)."""


def task_parameter(prefix: str, task: str) -> str:
    """SSM parameter name of ``task``'s registry entry in deployment ``prefix``: ``/<prefix>/models/tasks/<task>``.

    Raises:
        ValueError: ``task`` is not a task name (:data:`TASK_NAME`).
    """
    if not TASK_NAME.fullmatch(task):
        raise ValueError(f"task names are dot-separated lowercase words, got {task!r}")
    return f"/{prefix}/models/tasks/{task}"


class Effort(StrEnum):
    """Claude ``output_config.effort``."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class ModeEffort(BaseModel):
    """Effort per mode of the scheduler agent."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    chat: Effort = Effort.LOW
    """Effort of chat turns."""
    plan: Effort = Effort.MEDIUM
    """Effort of schedule building."""


class ModelRoute(StrEnum):
    """How a task reaches its model."""

    BEDROCK = "bedrock"


class Thinking(StrEnum):
    """Extended thinking: ``adaptive`` omits the field (the model's default); ``disabled`` turns it off."""

    ADAPTIVE = "adaptive"
    DISABLED = "disabled"


class DataClass(StrEnum):
    """What a task's prompts may contain: ``viewer`` data, or only ``operational`` data."""

    OPERATIONAL = "operational"
    VIEWER = "viewer"


class TaskSettings(BaseModel):
    """What a deployment configures for one task (``modules.models.tasks.<task>``)."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    model: str
    """Bedrock model id or inference profile; one of ``agent.allowed_models``, with ``{geo}`` as there."""
    route: ModelRoute = ModelRoute.BEDROCK
    """``bedrock``: the Converse API with the task's own role and application inference profile."""
    effort: Effort | ModeEffort = ModeEffort()
    """Reasoning effort: one level, or one per mode (``chat``, ``plan``) for the scheduler agent."""
    thinking: Thinking = Thinking.ADAPTIVE
    """Extended thinking; fixed per task, because changing it per call rewrites the prompt cache."""
    max_tokens: int = Field(default=16_000, ge=1, le=128_000)
    """Output token budget per model call."""
    data_class: DataClass = DataClass.OPERATIONAL
    """``viewer`` if the task's prompts carry viewer data, which keeps the model on a geographic inference profile
    (never ``global.``); swapping such a task's model needs human approval."""
    daily_cap_usd: float | None = Field(default=None, gt=0)
    """Daily model spend at which the task's token counter refuses further calls (about ten times expected use);
    ``null`` sets no cap."""
    eval: str | None = Field(default=None, pattern=EVAL_KEY)
    """Key of the passing evaluation record that qualifies ``model`` for this task."""

    @model_validator(mode="after")
    def _viewer_data_stays_in_its_geography(self) -> Self:
        if self.data_class is DataClass.VIEWER and self.model.startswith(GLOBAL_PROFILE_PREFIX):
            raise ValueError(
                f"model {self.model!r} is a global inference profile, which may process a request in any Region; "
                "a task with viewer data needs a geographic profile (us., eu., ...)"
            )
        return self


class TaskEntry(TaskSettings):
    """One task's registry entry, the SSM document that readers validate: the task's settings with ``{geo}`` resolved
    for the region, and the task's application inference profile."""

    profile_arn: str = Field(pattern=PROFILE_ARN)
    """The task's application inference profile, created by the deployment: the only model target its callers may
    invoke."""


def model_family(model: str) -> str:
    """The foundation model of ``model``, whatever profile routes it: ``us.anthropic.claude-haiku-5-5``,
    ``global.anthropic.claude-haiku-5-5`` and ``anthropic.claude-haiku-5-5`` are all ``anthropic.claude-haiku-5-5``."""
    prefix, _, family = model.partition(".")
    return family if prefix in _PROFILE_PREFIXES else model


def record_key(model: str, thinking: Thinking, max_tokens: int) -> str:
    """The eval record key of ``model`` with those settings: ``haiku-5-5-adaptive-16k`` for Claude Haiku 5.5 with
    adaptive thinking and 16,000 output tokens. Routing (``us.``, ``eu.``, ``global.``) is not part of it."""
    family = model_family(model).removeprefix("anthropic.").removeprefix("claude-")
    budget = f"{max_tokens // 1000}k" if max_tokens % 1000 == 0 else str(max_tokens)
    return re.sub(r"[^a-z0-9.-]+", "-", f"{family}-{thinking.value}-{budget}".lower()).strip("-")


class EvalCheck(BaseModel):
    """One check of an eval run."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    """What was checked, e.g. ``scenario_standard`` or ``faithfulness``."""
    passed: bool
    """Whether the run met the check's threshold."""
    value: float | None = None
    """The measured value (a share, a count or US dollars), when the check has one."""
    threshold: str | None = Field(default=None, max_length=40)
    """The pass condition, e.g. ``>= 0.95``, ``== 0`` or ``3/3 runs``."""


RECOMMENDER_CHECKS: Final = ("schema_valid", "faithfulness", "policy_violations", "cost_per_rail")
"""What a recommender task's record must hold (Phase 2, D§14.3): schema-valid rails; the deterministic faithfulness
check (each tile's reason cites only facts of the input, $0 to run); zero policy violations (kids, rating ceiling,
PII, candidate ids, consent); cost per rail. A judge sample (``judge_faithfulness``) is added before a default flip."""
REQUIRED_CHECKS: Final[Mapping[str, tuple[str, ...]]] = {
    SCHEDULER_TASK: ("scenario_standard", "scenario_hard"),
    "recommender": RECOMMENDER_CHECKS,
    "recommender.cohort": RECOMMENDER_CHECKS,
}
"""Checks a passing record of each task must include; tasks not listed need at least one check."""


class EvalRecord(BaseModel):
    """A conformance-suite run of one model on one task: the evidence that qualifies the model for the task
    (``evals/records/<task>/<key>.json``, committed). Written by ``make eval TASK=<task> MODEL=<id>``."""

    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)

    task: str = Field(pattern=TASK_NAME.pattern)
    """The task the suite tests."""
    key: str = Field(pattern=EVAL_KEY)
    """The record's key (its file name), which a registry entry names in ``eval``."""
    model: str
    """The model id that ran (any routing of the same foundation model qualifies)."""
    effort: Effort | ModeEffort
    """The effort the suite ran with."""
    thinking: Thinking
    """The thinking setting the suite ran with."""
    max_tokens: int = Field(ge=1, le=128_000)
    """The output token budget the suite ran with."""
    suite: str = Field(min_length=1, max_length=200)
    """The suite and its version, e.g. ``scheduler conformance v1: standard, hard``."""
    runs: int = Field(ge=1)
    """Runs per scenario or input set."""
    passed: bool
    """Every check passed."""
    checks: tuple[EvalCheck, ...] = Field(min_length=1)
    """The checks, each with its value and threshold."""
    cost_usd: Decimal = Field(ge=0)
    """Model spend of the whole run, at list prices."""
    recorded_at: AwareDatetime
    """When the run finished (UTC)."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.passed != all(check.passed for check in self.checks):
            raise ValueError("passed must be true exactly when every check passed")
        return self

    def problems(self, task: str, settings: TaskSettings) -> list[str]:
        """Why this record does not qualify ``settings`` (a registry entry) for ``task``; empty when it does."""
        problems = []
        if self.task != task:
            problems.append(f"it is a record of task {self.task!r}")
        if not self.passed:
            failed = ", ".join(check.name for check in self.checks if not check.passed)
            problems.append(f"it did not pass ({failed})")
        if model_family(self.model) != model_family(settings.model):
            problems.append(f"it tested {model_family(self.model)}, not {model_family(settings.model)}")
        ran = (self.effort, self.thinking, self.max_tokens)
        if ran != (settings.effort, settings.thinking, settings.max_tokens):
            problems.append(
                f"it ran with effort {_text(self.effort)}, thinking {self.thinking.value} and max_tokens "
                f"{self.max_tokens}, the entry has {_text(settings.effort)}, {settings.thinking.value} and "
                f"{settings.max_tokens}"
            )
        if missing := [name for name in REQUIRED_CHECKS.get(task, ()) if name not in {c.name for c in self.checks}]:
            problems.append(f"it lacks the checks {missing} that task {task} requires")
        return problems


def _text(effort: Effort | ModeEffort) -> str:
    match effort:
        case ModeEffort():
            return f"chat {effort.chat.value} / plan {effort.plan.value}"
        case _:
            return effort.value


def record_path(directory: Path, task: str, key: str) -> Path:
    """Where the record ``key`` of ``task`` lives under ``directory`` (``<task>/<key>.json``)."""
    return directory / task / f"{key}.json"


def qualification(directories: tuple[Path, ...], task: str, settings: TaskSettings) -> list[str]:
    """Why ``settings`` (with its ``eval`` key) is not qualified for ``task`` by a record in ``directories`` (the
    first that has the file); empty when it is."""
    if settings.eval is None:
        key = record_key(settings.model, settings.thinking, settings.max_tokens)
        return [f"names no eval record; run make eval TASK={task} MODEL=<model> and set eval (e.g. {key!r})"]
    path = next((d for d in directories if record_path(d, task, settings.eval).is_file()), None)
    if path is None:
        where = " or ".join(str(record_path(d, task, settings.eval)) for d in directories)
        return [f"eval record {settings.eval!r} does not exist ({where}); run make eval TASK={task} MODEL=<model>"]
    file = record_path(path, task, settings.eval)
    try:
        record = EvalRecord.model_validate_json(file.read_text())
    except ValidationError as error:
        return [f"eval record {file} is invalid: {error.error_count()} errors"]
    if record.key != settings.eval:
        return [f"eval record {file} has key {record.key!r}"]
    problems = record.problems(task, settings)
    return [f"eval record {settings.eval!r} does not qualify it: {problem}" for problem in problems]
