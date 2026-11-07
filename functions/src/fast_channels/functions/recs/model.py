"""One model rail: the ``recommender`` task's Converse call, its cost, and what went wrong if it failed.

The call is admitted by the task's spend counter first and its cost is added after, so a runaway stops at the daily
cap (:class:`~fast_channels.core.model_spend.SpendCounter`). Every failure is a :class:`ModelFailure` with the
:class:`~fast_channels.core.recs.router.Reason` the request is served the rule-based rail for: ``cap``, ``timeout``,
``model_error`` (throttling, access, a service error) or ``invalid`` (the output budget ran out, or the output is not
a rail).
"""

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError
from pydantic import BaseModel, ConfigDict, ValidationError

from fast_channels.core.model_spend import CapReached, cost
from fast_channels.core.recs import TASK
from fast_channels.core.recs.prompt import converse_request
from fast_channels.core.recs.router import Reason

if TYPE_CHECKING:
    from fast_channels.core.model_registry import TaskSettings
    from fast_channels.core.model_spend import SpendCounter

READ_TIMEOUT_S: Final = 30
"""A rail took 4.6 s at the median and 5.7 s at p95 in P0-24 with thinking disabled; 30 s is a stuck call."""


class TileOutput(BaseModel):
    """One tile of the model's structured output (``prompt.RAIL_SCHEMA``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: str
    reason: str
    evidence: tuple[str, ...]


class RailOutput(BaseModel):
    """The model's structured output (``prompt.RAIL_SCHEMA``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    tiles: tuple[TileOutput, ...]


@dataclass(frozen=True, slots=True)
class ModelRail:
    """A model call that returned a rail."""

    output: RailOutput
    usage: dict[str, int]
    cost_usd: Decimal
    latency_ms: int
    request_id: str


class ModelFailure(Exception):  # noqa: N818 - the reason a rail falls back, not an error of the caller
    """A model call that returned no rail, and why."""

    def __init__(self, reason: Reason, detail: str, *, cost_usd: Decimal = Decimal(0), request_id: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.cost_usd = cost_usd
        self.request_id = request_id


@cache
def runtime(region: str | None = None, read_timeout: int = READ_TIMEOUT_S) -> Any:
    """A Bedrock Runtime client: one retry, so a throttled rail falls back quickly instead of piling up."""
    config = Config(read_timeout=read_timeout, connect_timeout=5, retries={"mode": "standard", "max_attempts": 2})
    return boto3.client("bedrock-runtime", region_name=region, config=config)  # type: ignore[call-overload]


def call(
    settings: TaskSettings,
    model_id: str,
    *,
    catalog: str,
    request: str,
    client: Any,
    counter: SpendCounter | None = None,
    cap_usd: Decimal | None = None,
    task: str = TASK,
) -> ModelRail:
    """One rail from the model. ``model_id`` is the task's application inference profile; ``settings.model`` (the
    registry entry's model) prices it.

    Raises:
        ModelFailure: the call was refused, failed, or returned no rail.
    """
    if counter is not None and cap_usd is not None:
        try:
            counter.admit(task, cap_usd)
        except CapReached as refused:
            raise ModelFailure(Reason.CAP, refused.message) from None
    started = time.perf_counter()
    try:
        response = client.converse(**converse_request(settings, model_id, catalog=catalog, request=request))
    except (ReadTimeoutError, ConnectTimeoutError) as error:
        raise ModelFailure(Reason.TIMEOUT, str(error)) from None
    except ClientError as error:
        code = error.response.get("Error", {}).get("Code", "ClientError")
        request_id = error.response.get("ResponseMetadata", {}).get("RequestId", "")
        raise ModelFailure(Reason.MODEL_ERROR, code, request_id=request_id) from None
    except BotoCoreError as error:
        raise ModelFailure(Reason.MODEL_ERROR, type(error).__name__) from None
    usage = {key: int(value) for key, value in (response.get("usage") or {}).items() if isinstance(value, int)}
    spent = cost(settings.model, usage)
    if counter is not None:
        counter.add(task, spent)
    request_id = response.get("ResponseMetadata", {}).get("RequestId", "")
    latency_ms = int((response.get("metrics") or {}).get("latencyMs") or (time.perf_counter() - started) * 1000)
    blocks = ((response.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
    if response.get("stopReason") == "max_tokens":
        raise ModelFailure(Reason.INVALID, "the output budget ran out", cost_usd=spent, request_id=request_id)
    try:
        output = RailOutput.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as error:
        raise ModelFailure(
            Reason.INVALID, f"not a rail: {type(error).__name__}", cost_usd=spent, request_id=request_id
        ) from None
    return ModelRail(output=output, usage=usage, cost_usd=spent, latency_ms=latency_ms, request_id=request_id)
