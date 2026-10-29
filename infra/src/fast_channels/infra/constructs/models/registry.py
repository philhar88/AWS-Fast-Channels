"""The per-task model registry: one SSM parameter per task, rendered from ``modules.models.tasks``."""

import json
from collections.abc import Collection, Mapping
from typing import Final

from aws_cdk import aws_ssm as ssm
from constructs import Construct, IConstruct

from fast_channels.core.model_registry import TaskEntry, task_parameter
from fast_channels.infra.arns import arn
from fast_channels.infra.config import GEO_PLACEHOLDER

READ_ACTIONS: Final = ("ssm:GetParameter",)
"""What a reader of an entry needs: the agent reads its entry with ``GetParameter`` only."""


def render(entry: TaskEntry) -> str:
    """The SSM document of ``entry``: compact JSON of every field, in the entry's field order."""
    return json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))


class ModelRegistry(Construct):
    """``/<prefix>/models/tasks/<task>`` for every task, each holding its
    :class:`~fast_channels.core.model_registry.TaskEntry` as JSON.

    Model ids are deploy-time: every entry must name one of ``allowed_models`` (the models the deployment's roles
    may invoke), so a later edit of a parameter can only choose among them, and readers refuse anything else.

    Args:
        prefix: deployment prefix (the parameter path).
        tasks: registry entries by task name, with ``{geo}`` resolved for the region.
        allowed_models: the IaC allow-list (``agent.allowed_models``, resolved).

    Attributes:
        parameters: the parameter of each task.

    Raises:
        ValueError: a task's model is not in ``allowed_models``, or a model id still holds ``{geo}``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        tasks: Mapping[str, TaskEntry],
        allowed_models: Collection[str],
    ) -> None:
        super().__init__(scope, id)
        if unresolved := sorted(task for task, entry in tasks.items() if GEO_PLACEHOLDER in entry.model):
            raise ValueError(f"{unresolved}: resolve {{geo}} for the region first (FastChannelsConfig.for_region)")
        if outside := {task: entry.model for task, entry in tasks.items() if entry.model not in allowed_models}:
            raise ValueError(f"models outside allowed_models {sorted(allowed_models)}: {outside}")
        self.parameters: dict[str, ssm.StringParameter] = {
            task: ssm.StringParameter(
                self,
                task,
                parameter_name=task_parameter(prefix, task),
                description=f"Model registry entry of task {task} (JSON; readers validate it against the allow-list)",
                string_value=render(entry),
            )
            for task, entry in sorted(tasks.items())
        }


def entry_arn(scope: IConstruct, *, prefix: str, task: str) -> str:
    """ARN of ``task``'s registry entry, in ``scope``'s stack.

    A reader addresses the entry by name, so its stack takes no reference to the registry's stack: the registry is
    deployed first (a stack dependency) and the reader finds the entry by name.
    """
    return arn(scope, "ssm", "parameter", task_parameter(prefix, task).lstrip("/"))
