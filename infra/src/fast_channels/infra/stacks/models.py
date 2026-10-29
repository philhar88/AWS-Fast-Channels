"""``FastChannels-Models``: the per-task model registry, deployed when ``modules.models.enabled``."""

import json
from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack

from fast_channels.core.model_registry import task_parameter
from fast_channels.infra.constructs.models.caps import SpendCounters
from fast_channels.infra.constructs.models.profiles import TaskProfiles
from fast_channels.infra.constructs.models.registry import ModelRegistry
from fast_channels.infra.exports import ModelsExports

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class ModelsStack(Stack):
    """One application inference profile and one registry entry per LLM task (``modules.models.tasks``), and the
    tasks' token counters.

    - Each task's profile, ``<prefix>-<task>``, copies the system inference profile of the task's model and is tagged
      ``fast:task``: per-task ``AWS/Bedrock`` metrics (``ModelId`` = the profile id) and cost allocation.
    - Each task's entry is the SSM parameter ``/<prefix>/models/tasks/<task>``: model, route, request settings,
      daily cap, eval record and the profile's ARN.
    - ``<prefix>-models`` holds the per-task daily spend counters.

    Deployed before the Operator stack, whose scheduler agent invokes ``scheduler.plan``'s profile.

    Args:
        config: deployment configuration, validated for the stack's region (``{geo}`` resolved).

    Attributes:
        profiles, registry, counters: the constructs; exports: what the tasks' callers receive.
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): model registry", **kwargs)
        tasks = config.modules.models.tasks
        self.profiles = TaskProfiles(self, "Profiles", prefix=config.prefix, tasks=tasks)
        self.registry = ModelRegistry(
            self,
            "Registry",
            prefix=config.prefix,
            tasks=tasks,
            profiles={task: profile.arn for task, profile in self.profiles.profiles.items()},
            allowed_models=config.agent.allowed_models,
        )
        self.counters = SpendCounters(self, "Counters", prefix=config.prefix, removal_policy=config.removal_policy)
        self.exports = ModelsExports(profiles=dict(self.profiles.profiles))
        outputs = {
            "TaskParameters": json.dumps({task: task_parameter(config.prefix, task) for task in sorted(tasks)}),
            "TaskProfiles": self.to_json_string(
                {task: profile.arn for task, profile in sorted(self.profiles.profiles.items())}
            ),
            "CounterTableName": self.counters.table_name,
        }
        descriptions = {
            "TaskParameters": "Registry entry (SSM parameter name) of each task",
            "TaskProfiles": "Application inference profile ARN of each task",
            "CounterTableName": "DynamoDB table of the tasks' daily spend counters",
        }
        for name, value in outputs.items():
            CfnOutput(self, name, value=value, description=descriptions[name])
