"""Model selection: one validated ``ModelSpec`` in SSM, switchable without a redeploy.

The runtime-mutable spec is deliberately narrow (Bedrock model id from the deploy-time allow-list, effort per
mode, max tokens): a mutable provider or endpoint would let anyone with ``ssm:PutParameter`` redirect prompts and
tool results outside the IAM allow-list (_review.md, ModelSpec). Invalid specs fall back to the code default.
"""

import json
import logging
import threading
import time
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

import boto3
from pydantic import BaseModel, Field, ValidationError
from strands.models import BedrockModel, CacheConfig

if TYPE_CHECKING:
    from fast_channels.agent.protocol import Mode
    from fast_channels.agent.settings import Settings

logger = logging.getLogger(__name__)

SPEC_TTL_SECONDS = 60.0

type EffortLevel = Literal["low", "medium", "high", "max"]


class Effort(BaseModel, frozen=True, extra="forbid"):
    """Claude ``output_config.effort`` per mode: low suits chat, medium suits schedule building (models-ui.md §2.5)."""

    chat: EffortLevel = "low"
    plan: EffortLevel = "medium"


class ModelSpec(BaseModel, frozen=True, extra="forbid"):
    """The model the agent runs on.

    Attributes:
        model_id: Bedrock model or inference-profile id; must be in ``ALLOWED_MODELS``.
        effort: Reasoning effort per mode (sent to Anthropic models only).
        max_tokens: Output token budget per model call.
    """

    model_id: str
    effort: Effort = Effort()
    max_tokens: int = Field(default=16_000, ge=1, le=128_000)


class ModelSpecSource:
    """Resolves the active ``ModelSpec`` from SSM, cached for 60 s and validated against the allow-list."""

    def __init__(self, settings: Settings, *, ttl_seconds: float = SPEC_TTL_SECONDS) -> None:
        self._settings = settings
        self._default = ModelSpec(model_id=settings.default_model_id)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, ModelSpec] | None = None

    def current(self) -> ModelSpec:
        """The spec to use now (SSM value when valid, else the code default)."""
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self._ttl:
                return self._cached[1]
            spec = self._load()
            self._cached = (time.monotonic(), spec)
            return spec

    def _load(self) -> ModelSpec:
        name = self._settings.model_spec_parameter
        if name is None:
            return self._default
        try:
            raw = _ssm(self._settings.region).get_parameter(Name=name)["Parameter"]["Value"]
            return parse_spec(raw, allowed=self._settings.allowed_models)
        except Exception:
            logger.warning("model spec unavailable, using default", extra={"parameter": name}, exc_info=True)
            return self._default


def parse_spec(raw: str, *, allowed: frozenset[str]) -> ModelSpec:
    """Validate a ModelSpec JSON document; raises ``ValueError`` if invalid or the model is not allowed."""
    try:
        spec = ModelSpec.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"invalid model spec: {error}") from error
    if spec.model_id not in allowed:
        raise ValueError(f"model {spec.model_id!r} is not in ALLOWED_MODELS")
    return spec


@cache
def bedrock_model(spec: ModelSpec, mode: Mode, region: str) -> BedrockModel:
    """A Bedrock model for ``spec`` in ``mode`` (cached: the boto3 client is reused across requests).

    No ``temperature``: Claude 5.x rejects it (agentcore.md §12.7). ``region_name`` is always explicit because the
    Strands default region is us-west-2.
    """
    request_fields: dict[str, Any] | None = None
    if "anthropic.claude" in spec.model_id:
        request_fields = {"output_config": {"effort": getattr(spec.effort, mode.value)}}
    return BedrockModel(
        model_id=spec.model_id,
        max_tokens=spec.max_tokens,
        additional_request_fields=request_fields,
        cache_config=CacheConfig(strategy="auto"),
        region_name=region,
    )


@cache
def _ssm(region: str) -> Any:
    return boto3.client("ssm", region_name=region)
