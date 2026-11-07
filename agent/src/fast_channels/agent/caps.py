"""The daily spend cap of the agent's model-registry task (OD-03): ``fast_channels.core.model_spend``'s counter with
the agent's clients, refusal and Strands hooks.

The counter is one DynamoDB item per task and UTC day, ``COUNTER#<task>#<day>`` in the deployment's ``<prefix>-models``
table. A run starts only while the day's spend is under the task's cap (a conditional write), so does every model call
in it, and each call's cost is added from its token usage at list prices. Past the cap the run is refused with
``MODEL_CAP_REACHED`` and the alert topic is told once per task and day. Caps sit at about ten times expected use: they
stop a runaway, they are not a budget, so a counter that cannot be reached lets the run through with a warning.
"""

import asyncio
import time
from decimal import Decimal
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookRegistry

from fast_channels.agent.protocol import AgentRunError
from fast_channels.core import model_spend
from fast_channels.core.model_spend import CAP_REACHED, COUNTER_DAYS, GLOBAL_PRICES, CapReached, Price, cost, price

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = [
    "CAP_REACHED",
    "COUNTER_DAYS",
    "GLOBAL_PRICES",
    "CapGuard",
    "CapReachedError",
    "Price",
    "SpendCounter",
    "cost",
    "price",
]


class CapReachedError(AgentRunError):
    """The task's spend reached its daily cap: no model call until the next UTC day."""

    def __init__(self, task: str, cap_usd: Decimal) -> None:
        super().__init__(
            CAP_REACHED,
            f"Today's model spend of task {task} reached its cap of {cap_usd} US dollars; the agent resumes at 00:00 "
            "UTC, or when the cap is raised.",
        )


class SpendCounter(model_spend.SpendCounter):
    """Daily spend of model-registry tasks in the ``<prefix>-models`` table, refusing with :class:`CapReachedError`.

    Args:
        table: table name.
        topic_arn: alert topic told when a cap is reached (once per task and day); ``None`` tells nobody.
        region: AWS region of the table and topic.
        clock: seconds since the epoch (tests).
    """

    def __init__(
        self, *, table: str, topic_arn: str | None, region: str, clock: Callable[[], float] = time.time
    ) -> None:
        super().__init__(table=table, topic_arn=topic_arn, dynamodb=_dynamodb(region), sns=_sns(region), clock=clock)

    def admit(self, task: str, cap_usd: Decimal, *, counter: str = "calls") -> None:
        """Count one ``counter`` (``runs`` or ``calls``) if the day's spend is under ``cap_usd``.

        Raises:
            CapReachedError: the spend reached the cap (the alert topic is told the first time of the day).
        """
        try:
            super().admit(task, cap_usd, counter=counter)
        except CapReached:
            raise CapReachedError(task, cap_usd) from None


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
