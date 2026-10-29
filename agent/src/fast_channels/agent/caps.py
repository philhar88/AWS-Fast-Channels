"""The daily spend cap of the agent's model-registry task: a deterministic token counter, a circuit breaker (OD-03).

The counter is one DynamoDB item per task and UTC day, ``COUNTER#<task>#<day>`` in the deployment's ``<prefix>-models``
table. A run starts only while the day's spend is under the task's cap (a conditional write), so does every model call
in it, and each call's cost is added from its token usage at list prices. Past the cap the run is refused with
``MODEL_CAP_REACHED`` and the alert topic is told once per task and day. Caps sit at about ten times expected use: they
stop a runaway, they are not a budget, so a counter that cannot be reached lets the run through with a warning.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING, Any, Final

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookRegistry

from fast_channels.agent.protocol import AgentRunError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

CAP_REACHED: Final = "MODEL_CAP_REACHED"
COUNTER_DAYS: Final = 35
"""Days a counter item is kept (DynamoDB TTL ``expires_at``)."""
_PER_MILLION: Final = Decimal(1_000_000)
_GEOGRAPHIC_MARKUP: Final = Decimal("1.1")
"""Geographic (``us.``, ``eu.``, ...) and in-Region requests cost 10% more than ``global.`` ones (Price List)."""
_PROFILE_PREFIXES: Final = frozenset({"global", "us", "eu", "apac", "ca", "jp", "au", "us-gov"})


@dataclass(frozen=True, slots=True)
class Price:
    """US dollars per million tokens: input, output, cache read, cache write (5 min)."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def __mul__(self, factor: Decimal) -> Price:
        return Price(self.input * factor, self.output * factor, self.cache_read * factor, self.cache_write * factor)


GLOBAL_PRICES: Final = {
    "anthropic.claude-haiku-5-5": Price(Decimal("0.10"), Decimal("0.50"), Decimal("0.01"), Decimal("0.125")),
    "anthropic.claude-sonnet-5-5": Price(Decimal("2.00"), Decimal("10.00"), Decimal("0.10"), Decimal("2.50")),
    "anthropic.claude-opus-5-5": Price(Decimal("4.00"), Decimal("20.00"), Decimal("0.20"), Decimal("5.00")),
}
"""``global.`` prices of the models the deployment allows by default and the Opus judge (AWS Price List API,
us-east-1, standard tier, 2026-10-27). A model missing here is counted at the most expensive one's prices, so its cap
trips early rather than late."""


def price(model_id: str) -> Price:
    """List price of ``model_id`` (a ``<geo>.`` or ``global.`` inference profile, or a foundation model id)."""
    prefix, _, family = model_id.partition(".")
    if prefix not in _PROFILE_PREFIXES:
        prefix, family = "", model_id
    base = GLOBAL_PRICES.get(family)
    if base is None:
        logger.warning("no list price for the model, counting it at the highest", extra={"model_id": model_id})
        base = max(GLOBAL_PRICES.values(), key=lambda p: p.output)
    return base if prefix == "global" else base * _GEOGRAPHIC_MARKUP


def cost(model_id: str, usage: Mapping[str, int]) -> Decimal:
    """Cost of one model call from its Converse ``usage`` (``inputTokens`` excludes cache reads and writes)."""
    p = price(model_id)
    total = (
        usage.get("inputTokens", 0) * p.input
        + usage.get("outputTokens", 0) * p.output
        + usage.get("cacheReadInputTokens", 0) * p.cache_read
        + usage.get("cacheWriteInputTokens", 0) * p.cache_write
    )
    return total / _PER_MILLION


class CapReachedError(AgentRunError):
    """The task's spend reached its daily cap: no model call until the next UTC day."""

    def __init__(self, task: str, cap_usd: Decimal) -> None:
        super().__init__(
            CAP_REACHED,
            f"Today's model spend of task {task} reached its cap of {cap_usd} US dollars; the agent resumes at 00:00 "
            "UTC, or when the cap is raised.",
        )


class SpendCounter:
    """Daily spend of model-registry tasks in the ``<prefix>-models`` table.

    Args:
        table: table name.
        topic_arn: alert topic told when a cap is reached (once per task and day); ``None`` tells nobody.
        region: AWS region of the table and topic.
        clock: seconds since the epoch (tests).
    """

    def __init__(
        self, *, table: str, topic_arn: str | None, region: str, clock: Callable[[], float] = time.time
    ) -> None:
        self._table = table
        self._topic_arn = topic_arn
        self._region = region
        self._clock = clock

    def admit(self, task: str, cap_usd: Decimal, *, counter: str = "calls") -> None:
        """Count one ``counter`` (``runs`` or ``calls``) if the day's spend is under ``cap_usd``.

        Raises:
            CapReachedError: the spend reached the cap (the alert topic is told the first time of the day).
        """
        key, expires = self._key(task)
        try:
            _dynamodb(self._region).update_item(
                TableName=self._table,
                Key={"pk": {"S": key}},
                UpdateExpression="ADD #counter :one SET expires_at = if_not_exists(expires_at, :expires)",
                ConditionExpression="attribute_not_exists(spent_usd) OR spent_usd < :cap",
                ExpressionAttributeNames={"#counter": counter},
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":expires": {"N": str(expires)},
                    ":cap": {"N": str(cap_usd)},
                },
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning("model spend counter unavailable, not enforcing the cap", exc_info=True)
                return
            self._notify_once(task, key, cap_usd)
            raise CapReachedError(task, cap_usd) from None
        except BotoCoreError:
            logger.warning("model spend counter unavailable, not enforcing the cap", exc_info=True)

    def add(self, task: str, usd: Decimal) -> None:
        """Add ``usd`` to the day's spend of ``task``."""
        if usd <= 0:
            return
        key, expires = self._key(task)
        try:
            _dynamodb(self._region).update_item(
                TableName=self._table,
                Key={"pk": {"S": key}},
                UpdateExpression="ADD spent_usd :usd SET expires_at = if_not_exists(expires_at, :expires)",
                ExpressionAttributeValues={":usd": {"N": str(usd)}, ":expires": {"N": str(expires)}},
            )
        except ClientError, BotoCoreError:
            logger.warning("model spend counter unavailable, a call's cost is not counted", exc_info=True)

    def _notify_once(self, task: str, key: str, cap_usd: Decimal) -> None:
        try:
            _dynamodb(self._region).update_item(
                TableName=self._table,
                Key={"pk": {"S": key}},
                UpdateExpression="SET notified_at = :now",
                ConditionExpression="attribute_not_exists(notified_at)",
                ExpressionAttributeValues={":now": {"N": str(int(self._clock()))}},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                logger.warning("model spend counter unavailable, cap notification skipped", exc_info=True)
            return
        logger.warning("model spend cap reached", extra={"task": task, "cap_usd": str(cap_usd), "counter": key})
        if self._topic_arn is None:
            return
        try:
            _sns(self._region).publish(
                TopicArn=self._topic_arn,
                Subject=f"FAST Channels: model spend cap of {task} reached"[:100],
                Message=(
                    f"Today's model spend of task {task} reached its daily cap of {cap_usd} US dollars, so its model "
                    f"calls are refused until 00:00 UTC. Counter item: {key}. Raise daily_cap_usd in the task's "
                    "registry entry (modules.models.tasks) if the use is expected."
                ),
            )
        except ClientError, BotoCoreError:
            logger.warning("cap notification could not be published", exc_info=True)

    def _key(self, task: str) -> tuple[str, int]:
        now = self._clock()
        day = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d")
        return f"COUNTER#{task}#{day}", int(now) + COUNTER_DAYS * 86_400


class CapGuard:
    """Strands hooks that keep a run's model calls under the task's cap: each call is admitted first and its cost
    counted after. A call past the cap is cancelled with the refusal as the model's reply, which ends the run."""

    def __init__(self, counter: SpendCounter, *, task: str, cap_usd: Decimal, model_id: str) -> None:
        self._counter = counter
        self._task = task
        self._cap = cap_usd
        self._model_id = model_id

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)
        registry.add_callback(AfterModelCallEvent, self._after_model_call)

    async def _before_model_call(self, event: BeforeModelCallEvent) -> None:
        try:
            await asyncio.to_thread(self._counter.admit, self._task, self._cap)
        except CapReachedError as refused:
            event.cancel = refused.message

    async def _after_model_call(self, event: AfterModelCallEvent) -> None:
        if event.stop_response is None:
            return
        metadata: Mapping[str, Any] = event.stop_response.message.get("metadata") or {}  # type: ignore[assignment]
        if usage := metadata.get("usage"):
            await asyncio.to_thread(self._counter.add, self._task, cost(self._model_id, usage))


@cache
def _dynamodb(region: str) -> Any:
    return boto3.client("dynamodb", region_name=region)


@cache
def _sns(region: str) -> Any:
    return boto3.client("sns", region_name=region)
