"""Runtime configuration, read once from the environment the infra stack sets on the AgentCore Runtime."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

FALLBACK_MODEL_ID = "us.anthropic.claude-haiku-5-5"
"""Used only when ``DEFAULT_MODEL_ID`` is unset (local runs); infra always sets the region's geo profile."""


class SettingsError(RuntimeError):
    """A required environment variable is missing."""


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(item.strip() for item in (value or "").split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment of the scheduling agent.

    Attributes:
        region: AWS region for Bedrock, SSM and Memory clients (``AWS_REGION``).
        gateway_url: AgentCore Gateway MCP endpoint (``GATEWAY_URL``).
        memory_id: AgentCore Memory id (``MEMORY_ID``); ``None`` keeps sessions on local disk (dev only).
        preference_namespace: Namespace template of the USER_PREFERENCE strategy, e.g.
            ``/users/{actorId}/preferences/`` (``MEMORY_PREFERENCE_NAMESPACE``); ``None`` disables recall.
        confirm_tools: Qualified tool names that need operator approval (``CONFIRM_TOOLS``, csv).
        model_spec_parameter: SSM parameter holding the ModelSpec JSON (``MODEL_SPEC_PARAMETER``).
        model_task_parameter: SSM parameter holding the agent's model-registry entry, read before the ModelSpec
            (``MODEL_TASK_PARAMETER``; set when the deployment has the model registry).
        default_model_id: Code default model (``DEFAULT_MODEL_ID``).
        allowed_models: Model ids the runtime role may invoke (``ALLOWED_MODELS``, csv); always includes the
            default.
    """

    region: str
    gateway_url: str
    memory_id: str | None = None
    preference_namespace: str | None = None
    confirm_tools: frozenset[str] = frozenset()
    model_spec_parameter: str | None = None
    model_task_parameter: str | None = None
    default_model_id: str = FALLBACK_MODEL_ID
    allowed_models: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        """Build settings from ``env``; raises ``SettingsError`` when the region or Gateway URL is missing."""
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        gateway_url = env.get("GATEWAY_URL")
        if not region or not gateway_url:
            raise SettingsError("AWS_REGION and GATEWAY_URL must be set")
        default_model_id = env.get("DEFAULT_MODEL_ID") or FALLBACK_MODEL_ID
        return cls(
            region=region,
            gateway_url=gateway_url,
            memory_id=env.get("MEMORY_ID") or None,
            preference_namespace=env.get("MEMORY_PREFERENCE_NAMESPACE") or None,
            confirm_tools=_csv(env.get("CONFIRM_TOOLS")),
            model_spec_parameter=env.get("MODEL_SPEC_PARAMETER") or None,
            model_task_parameter=env.get("MODEL_TASK_PARAMETER") or None,
            default_model_id=default_model_id,
            allowed_models=_csv(env.get("ALLOWED_MODELS")) | {default_model_id},
        )
