"""Model selection: the agent's model-registry entry or a ``ModelSpec`` in SSM, switchable without a redeploy.

The runtime-mutable documents are deliberately narrow (a Bedrock model id from the deploy-time allow-list, effort
per mode, thinking, max tokens): a mutable provider or endpoint would let anyone with ``ssm:PutParameter`` redirect
prompts and tool results outside the IAM allow-list (_review.md, ModelSpec). The first valid source wins:

1. the ``scheduler.plan`` entry of the model registry (``MODEL_TASK_PARAMETER``, deployed with ``modules.models``);
2. the ModelSpec (``MODEL_SPEC_PARAMETER``, deployed without the model registry);
3. the default: the registry entry as deployed (``MODEL_TASK_DEFAULT``), or the code default.

A source that is missing, unreadable, invalid or names a model outside the allow-list is skipped with a warning.
With the model registry the deployment also gives the task an application inference profile (``MODEL_PROFILE_ARN``):
every call then goes through it, whatever the source, and an entry must name exactly that profile.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING, Any, Final, Literal

import boto3
from pydantic import BaseModel, Field, ValidationError
from strands.models import BedrockModel, CacheConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from fast_channels.agent.protocol import Mode
    from fast_channels.agent.settings import Settings

logger = logging.getLogger(__name__)

SPEC_TTL_SECONDS = 60.0
TASK: Final = "scheduler.plan"
"""The agent's task in the model registry."""

type EffortLevel = Literal["low", "medium", "high", "max"]
type ThinkingMode = Literal["adaptive", "disabled"]
"""``adaptive`` omits the field (the model's default); ``disabled`` sends ``thinking: {type: disabled}``."""
PROFILE_ARN: Final = r"^arn:aws[a-z-]*:bedrock:[a-z0-9-]+:\d{12}:application-inference-profile/[a-z0-9]+$"


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
        thinking: Extended thinking (Anthropic models only); fixed for the agent, because changing it between calls
            rewrites the prompt cache.
    """

    model_id: str
    effort: Effort = Effort()
    max_tokens: int = Field(default=16_000, ge=1, le=128_000)
    thinking: ThinkingMode = "adaptive"


class TaskEntry(BaseModel, frozen=True, extra="forbid"):
    """A model-registry entry, the agent's own reader of ``contracts/model-registry.schema.json``.

    The agent never imports ``fast_channels.core``, which defines the entry; a contract test keeps the two schemas
    identical. Only the fields that shape a model call reach the :class:`ModelSpec`: the others (``data_class``,
    ``eval``) are validated here and acted on by the deployment; ``daily_cap_usd`` is the run's cap.
    """

    model: str
    route: Literal["bedrock"] = "bedrock"
    effort: EffortLevel | Effort = Effort()
    thinking: ThinkingMode = "adaptive"
    max_tokens: int = Field(default=16_000, ge=1, le=128_000)
    data_class: Literal["operational", "viewer"] = "operational"
    daily_cap_usd: float | None = Field(default=None, gt=0)
    eval: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    profile_arn: str = Field(pattern=PROFILE_ARN)

    def spec(self) -> ModelSpec:
        """The entry as the spec the agent runs on (one effort level applies to both modes)."""
        effort = self.effort if isinstance(self.effort, Effort) else Effort(chat=self.effort, plan=self.effort)
        return ModelSpec(model_id=self.model, effort=effort, max_tokens=self.max_tokens, thinking=self.thinking)


class SpecSource(StrEnum):
    """Where the active spec came from (logged with every run)."""

    REGISTRY = "registry"
    MODEL_SPEC = "model-spec"
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class ActiveSpec:
    """The spec to run on, its source and the daily spend cap that applies (``None``: none)."""

    spec: ModelSpec
    source: SpecSource
    daily_cap_usd: Decimal | None = None


class ModelSpecSource:
    """Resolves the active ``ModelSpec`` from SSM, cached for 60 s and validated against the allow-list."""

    def __init__(self, settings: Settings, *, ttl_seconds: float = SPEC_TTL_SECONDS) -> None:
        self._settings = settings
        self._default = ModelSpec(model_id=settings.default_model_id)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, ActiveSpec] | None = None

    def current(self) -> ActiveSpec:
        """The spec to use now: the first valid of the registry entry, the ModelSpec and the code default."""
        with self._lock:
            if self._cached and time.monotonic() - self._cached[0] < self._ttl:
                return self._cached[1]
            active = self._load()
            self._cached = (time.monotonic(), active)
            return active

    def _load(self) -> ActiveSpec:
        settings = self._settings
        readers: tuple[tuple[SpecSource, str | None, Callable[[str], ActiveSpec]], ...] = (
            (SpecSource.REGISTRY, settings.model_task_parameter, self._registry_entry),
            (SpecSource.MODEL_SPEC, settings.model_spec_parameter, self._model_spec),
        )
        for source, name, read in readers:
            if name is None:
                continue
            try:
                raw: str = _ssm(settings.region).get_parameter(Name=name)["Parameter"]["Value"]
                return read(raw)
            except Exception:
                logger.warning(
                    "model source unavailable, trying the next one",
                    extra={"source": source.value, "parameter": name},
                    exc_info=True,
                )
        if deployed := settings.model_task_default:
            try:
                return self._entry(deployed, SpecSource.DEFAULT)
            except ValueError:
                logger.warning("the deployed registry entry is invalid, using the code default", exc_info=True)
        return ActiveSpec(self._default, SpecSource.DEFAULT)

    def _registry_entry(self, raw: str) -> ActiveSpec:
        return self._entry(raw, SpecSource.REGISTRY)

    def _entry(self, raw: str, source: SpecSource) -> ActiveSpec:
        settings = self._settings
        entry = parse_task_entry(raw, allowed=settings.allowed_models, profile_arn=settings.model_profile_arn)
        cap = Decimal(str(entry.daily_cap_usd)) if entry.daily_cap_usd is not None else None
        return ActiveSpec(entry.spec(), source, cap)

    def _model_spec(self, raw: str) -> ActiveSpec:
        return ActiveSpec(parse_spec(raw, allowed=self._settings.allowed_models), SpecSource.MODEL_SPEC)


def parse_spec(raw: str, *, allowed: frozenset[str]) -> ModelSpec:
    """Validate a ModelSpec JSON document; raises ``ValueError`` if invalid or the model is not allowed."""
    try:
        spec = ModelSpec.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"invalid model spec: {error}") from error
    return _allowed(spec, allowed)


def parse_task_entry(raw: str, *, allowed: frozenset[str], profile_arn: str | None) -> TaskEntry:
    """Validate a model-registry entry; raises ``ValueError`` if it is invalid, its model is not allowed, or it names
    another profile than the deployment's ``profile_arn``."""
    try:
        entry = TaskEntry.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ValueError(f"invalid {TASK} registry entry: {error}") from error
    _allowed(entry.spec(), allowed)
    if entry.profile_arn != profile_arn:
        raise ValueError(f"registry entry names profile {entry.profile_arn!r}, not this deployment's {profile_arn!r}")
    return entry


def _allowed(spec: ModelSpec, allowed: frozenset[str]) -> ModelSpec:
    if spec.model_id not in allowed:
        raise ValueError(f"model {spec.model_id!r} is not in ALLOWED_MODELS")
    return spec


@cache
def bedrock_model(spec: ModelSpec, mode: Mode, region: str, profile_arn: str | None = None) -> BedrockModel:
    """A Bedrock model for ``spec`` in ``mode``, invoked through ``profile_arn`` when given (cached: the boto3 client
    is reused across requests).

    No ``temperature``: Claude 5.x rejects it (agentcore.md §12.7). ``region_name`` is always explicit because the
    Strands default region is us-west-2. Strands derives defaults from the model id string (prompt caching, tool
    result status, context window); a profile ARN names no model, so with a profile those are set from
    ``spec.model_id`` instead.
    """
    anthropic = "anthropic.claude" in spec.model_id
    request_fields: dict[str, Any] | None = None
    if anthropic:
        request_fields = {"output_config": {"effort": getattr(spec.effort, mode.value)}}
        if spec.thinking == "disabled":
            request_fields["thinking"] = {"type": "disabled"}
    model = BedrockModel(
        model_id=spec.model_id,
        max_tokens=spec.max_tokens,
        additional_request_fields=request_fields,
        cache_config=CacheConfig(strategy="anthropic" if anthropic and profile_arn else "auto"),
        include_tool_result_status=True if anthropic and profile_arn else "auto",
        region_name=region,
    )
    if profile_arn:
        model.update_config(model_id=profile_arn, context_window_limit=model.context_window_limit)
    return model


@cache
def _ssm(region: str) -> Any:
    return boto3.client("ssm", region_name=region)
