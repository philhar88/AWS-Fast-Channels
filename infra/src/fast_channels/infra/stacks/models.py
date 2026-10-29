"""``FastChannels-Models``: the per-task model registry, deployed when ``modules.models.enabled``."""

import json
from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack

from fast_channels.core.model_registry import task_parameter
from fast_channels.infra.constructs.models.registry import ModelRegistry

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig


class ModelsStack(Stack):
    """One registry entry per LLM task (``modules.models.tasks``), each a validated SSM parameter
    ``/<prefix>/models/tasks/<task>`` that names its model, route and fixed request settings.

    Deployed before the Operator stack, whose scheduler agent reads ``scheduler.plan``. The stack holds no state
    and takes no references from other stacks.

    Args:
        config: deployment configuration, validated for the stack's region (``{geo}`` resolved).

    Attributes:
        registry: the task entries.
    """

    def __init__(self, scope: Construct, id: str, *, config: FastChannelsConfig, **kwargs: Any) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): model registry", **kwargs)
        self.registry = ModelRegistry(
            self,
            "Registry",
            prefix=config.prefix,
            tasks=config.modules.models.tasks,
            allowed_models=config.agent.allowed_models,
        )
        CfnOutput(
            self,
            "TaskParameters",
            value=json.dumps({task: task_parameter(config.prefix, task) for task in self.registry.parameters}),
            description="Registry entry (SSM parameter name) of each task",
        )
