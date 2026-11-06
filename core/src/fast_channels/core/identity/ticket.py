"""The playback ticket and its row (DESIGN-v2.2 §5.1; ADR-0018).

A ticket is ``secrets.token_urlsafe(16)``: 22 URL-safe characters (128 random bits), redrawn while it contains ``--``
or ``__``. MediaTailor does not need that rule, but its User Guide still states a "double characters" rule, and the
redraw is free (P0-05: 0.96 % of draws, at most 3). The edge keeps the init query's ``ads.t`` only when it is one
:data:`TICKET` value.

The row ``T#<t>`` holds what the session may use, computed by the mint and never by the player: the viewer's ``vid``,
the privacy mode, the demo persona, the playback configuration the ticket is for, and the redeem window. Times are
epoch milliseconds, the API Gateway redeem's ``$context.requestTimeEpoch``; ``ttl`` is epoch seconds for DynamoDB.
"""

import re
import secrets
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

TICKET: Final = re.compile(r"[A-Za-z0-9_-]{22}")
"""A playback ticket, exactly as the edge and the redeem accept it."""
KEY_PREFIX: Final = "T#"
REDEEM_WINDOW_MS: Final = 120_000
"""From mint to the latest redeem: the player opens the URL right away."""
ROW_TTL_S: Final = 25 * 3600
"""DynamoDB deletes a row a day after the longest binding lease (24 h)."""
DOUBLES: Final = ("--", "__")
REDEEM_CONDITION: Final = (
    "attribute_exists(pk) AND pc = :pc AND ((attribute_not_exists(redeemedBy) AND redeem_by > :now) "
    "OR redeemedBy = :sid)"
)
"""Binds an unredeemed ticket inside its window to the session ``:sid`` of configuration ``:pc``; again for the same
session (a retried hook), never for another (DESIGN-v2.2 §5.3)."""
REDEEM_UPDATE: Final = "SET redeemedBy = :sid, bound_at = if_not_exists(bound_at, :now), last_seen = :now"


class PrivacyMode(StrEnum):
    """What the ticket's viewer agreed to (DESIGN-v2.2 §5.5), as the mint records it. The Pod Decision Service decides
    each break's mode from it and the binding."""

    FULL = "FULL"
    PSEUDONYMOUS = "PSEUDONYMOUS"
    OPTED_OUT = "OPTED_OUT"


def mint() -> str:
    """A fresh ticket: :func:`secrets.token_urlsafe` of 16 bytes, redrawn while it holds ``--`` or ``__``."""
    ticket = secrets.token_urlsafe(16)
    while any(double in ticket for double in DOUBLES):
        ticket = secrets.token_urlsafe(16)
    return ticket


def key(ticket: str) -> str:
    """The row key of ``ticket``: ``T#<t>``.

    Raises:
        ValueError: ``ticket`` is not a :data:`TICKET`.
    """
    if not TICKET.fullmatch(ticket):
        raise ValueError("not a playback ticket")
    return f"{KEY_PREFIX}{ticket}"


class TicketRow(BaseModel):
    """A ticket row as the mint writes it (the redeem adds ``redeemedBy``, ``bound_at`` and ``last_seen``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pk: str = Field(pattern=r"^T#[A-Za-z0-9_-]{22}$")
    pc: str = Field(min_length=1, max_length=64, description="The playback configuration the ticket is for.")
    pm: PrivacyMode
    cid: str = Field(min_length=1, max_length=64, description="Correlation id of the mint.")
    iat: int = Field(ge=0, description="Minted at, epoch ms.")
    redeem_by: int = Field(ge=0, description="The latest redeem, epoch ms.")
    ttl: int = Field(ge=0, description="DynamoDB expiry, epoch s.")
    vid: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    persona: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{0,31}$")

    def item(self) -> dict[str, Any]:
        """The row as a DynamoDB resource item: fields that are ``None`` are left out."""
        return self.model_dump(mode="json", exclude_none=True)


def ticket_row(
    *,
    ticket: str,
    pc: str,
    cid: str,
    now_ms: int,
    pm: PrivacyMode = PrivacyMode.PSEUDONYMOUS,
    vid: str | None = None,
    persona: str | None = None,
    redeem_window_ms: int = REDEEM_WINDOW_MS,
) -> TicketRow:
    """The row of a ticket minted at ``now_ms``."""
    return TicketRow(
        pk=key(ticket),
        pc=pc,
        pm=pm,
        cid=cid,
        iat=now_ms,
        redeem_by=now_ms + redeem_window_ms,
        ttl=now_ms // 1000 + ROW_TTL_S,
        vid=vid,
        persona=persona,
    )
