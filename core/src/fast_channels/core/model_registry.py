"""The per-task model registry: one validated entry per LLM task, stored as SSM ``/<prefix>/models/tasks/<task>``.

Each task (``scheduler.plan`` for the scheduling agent; Phase-2 tasks such as ``recommender``) names its Bedrock
model, how it reaches it, and the request settings that stay fixed for the task. Model ids are chosen only among the
deployment's IAM allow-list (``agent.allowed_models``): the registry is data, the allow-list is the security
property, so a registry edit can only choose among models the deployment already grants. Readers validate every
entry and refuse one outside the allow-list.

``thinking`` and the output schema of a task never change per call: either change rewrites the prompt cache (a
per-call schema tripled the cost per call in the design measurements).
"""

import re
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEDULER_TASK: Final = "scheduler.plan"
"""The scheduling agent's task (the AgentCore Runtime in the operator plane)."""
TASK_NAME: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
"""Task names: dot-separated lowercase words (``scheduler.plan``, ``recommender.cohort``)."""
GLOBAL_PROFILE_PREFIX: Final = "global."
"""Global cross-Region inference profiles route a request to any commercial Region in the world."""


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


class TaskEntry(BaseModel):
    """One task's registry entry: configured as ``modules.models.tasks.<task>`` and stored as the SSM document that
    readers validate, with ``{geo}`` resolved for the region."""

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
    eval: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    """Key of the passing evaluation record that qualifies ``model`` for this task."""

    @model_validator(mode="after")
    def _viewer_data_stays_in_its_geography(self) -> Self:
        if self.data_class is DataClass.VIEWER and self.model.startswith(GLOBAL_PROFILE_PREFIX):
            raise ValueError(
                f"model {self.model!r} is a global inference profile, which may process a request in any Region; "
                "a task with viewer data needs a geographic profile (us., eu., ...)"
            )
        return self
