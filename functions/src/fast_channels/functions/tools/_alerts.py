"""Channel Assembly alerts (ListAlerts) without the warnings this design raises by construction.

Two PLAYBACK_WARNING codes are expected: ``SEGMENT_DURATION_MISMATCH`` (2 s ESAM split segments next to 6 s ones) and
``BANDWIDTH_MISMATCH`` (QVBR slates next to content). One 13-program e2e run raised 132 and 64 of them, burying the
real alerts (e2e.md §3 and §9 item 13). On a channel ARN, Channel Assembly reports each program alert as
``PROGRAM_ALERT`` with the underlying code in the message, so the filter reads both.
"""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field

from fast_channels.functions import aws
from fast_channels.functions.media_library import iso

NOISE_ALERTS: Final = frozenset({"SEGMENT_DURATION_MISMATCH", "BANDWIDTH_MISMATCH"})
PROGRAM_ALERT: Final = "PROGRAM_ALERT"
MAX_ALERTS: Final = 20
"""Alerts a write tool returns after its change (``origins___list_alerts`` pages up to 100)."""
UNAVAILABLE: Final = "ALERTS_UNAVAILABLE"
"""Code of the INFO entry that :func:`after_change` returns when the alerts could not be read."""
_MESSAGE_CHARS: Final = 400
_CATEGORY_ORDER: Final = {"SCHEDULING_ERROR": 0, "PLAYBACK_WARNING": 1, "INFO": 2}

logger = Logger(child=True)


class Alert(BaseModel):
    """One Channel Assembly alert."""

    code: str = Field(description="Alert code; for PROGRAM_ALERT the message names the program and its alert.")
    category: str = Field(description="SCHEDULING_ERROR (breaks playback, fix it), PLAYBACK_WARNING or INFO.")
    message: str
    resource_arn: str
    last_modified: str = Field(description="UTC ISO-8601 time of the last change.")


def is_noise(item: Mapping[str, Any]) -> bool:
    """Whether a ListAlerts item is one of the expected warnings, directly or wrapped as a program alert."""
    code = item["AlertCode"]
    message = str(item.get("AlertMessage", ""))
    return code in NOISE_ALERTS or (code == PROGRAM_ALERT and any(noise in message for noise in NOISE_ALERTS))


def alerts_of(resource_arn: str, *, include_noise: bool = False) -> tuple[list[Alert], int]:
    """Alerts of ``resource_arn``, most severe and then newest first, and how many expected warnings were left out."""
    pages = aws.client("mediatailor").get_paginator("list_alerts").paginate(ResourceArn=resource_arn)
    items = [item for page in pages for item in page.get("Items", [])]
    kept = [item for item in items if include_noise or not is_noise(item)]
    kept.sort(key=lambda item: item["LastModifiedTime"], reverse=True)
    kept.sort(key=lambda item: _CATEGORY_ORDER.get(item.get("Category", ""), len(_CATEGORY_ORDER)))
    return [_alert(item, resource_arn) for item in kept], len(items) - len(kept)


def relevant(resource_arn: str) -> list[Alert]:
    """The :data:`MAX_ALERTS` most severe alerts of ``resource_arn``, expected warnings left out."""
    return alerts_of(resource_arn)[0][:MAX_ALERTS]


def after_change(resource_arn: Callable[[], str]) -> list[Alert]:
    """:func:`relevant` alerts of the resource a write tool has just changed.

    The change has happened by then, so failing to read its alerts must not report it as failed: the error is
    logged, and an INFO entry coded :data:`UNAVAILABLE` tells the agent to check the alerts itself.
    """
    try:
        return relevant(resource_arn())
    except (ClientError, BotoCoreError) as error:
        logger.warning("alerts could not be read after the change", exc_info=True)
        return [
            Alert(
                code=UNAVAILABLE,
                category="INFO",
                message=f"The change succeeded, but its alerts could not be read ({type(error).__name__}): "
                "check them with origins___list_alerts.",
                resource_arn="",
                last_modified=iso(datetime.now(UTC)),
            )
        ]


def _alert(item: Mapping[str, Any], resource_arn: str) -> Alert:
    return Alert(
        code=item["AlertCode"],
        category=item.get("Category", ""),
        message=str(item.get("AlertMessage", ""))[:_MESSAGE_CHARS],
        resource_arn=item.get("ResourceArn", resource_arn),
        last_modified=iso(item["LastModifiedTime"]),
    )
