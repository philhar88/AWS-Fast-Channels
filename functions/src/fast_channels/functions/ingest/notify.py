"""Publishes pipeline events (``Asset Ready``, ``Ladder Failed``, ...) on the deployment's ``fast-channels`` bus."""

from typing import TYPE_CHECKING

from fast_channels.core.events import put_events_entry
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from pydantic import BaseModel


class EventDeliveryError(RuntimeError):
    """EventBridge rejected the entry (PutEvents reports per-entry failures instead of raising)."""


def put_event(detail: BaseModel) -> None:
    """Put one event with ``detail`` (a :data:`fast_channels.core.events.DETAIL_TYPES` model) on the bus."""
    config = settings()
    config.require("prefix", "event_bus")
    entry = put_events_entry(detail, prefix=config.prefix, event_bus=config.event_bus)
    response = aws.client("events").put_events(Entries=[entry])
    if response.get("FailedEntryCount"):
        failure = response["Entries"][0]
        raise EventDeliveryError(f"{entry['DetailType']}: {failure.get('ErrorCode')} {failure.get('ErrorMessage')}")
