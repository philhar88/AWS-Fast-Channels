"""``sales`` target (M9; ``modules.agents`` with ``modules.pds``): the own book's pacing, and proposals about its lines.

The tools of the sales agent, which is propose-only (L1, ADR-0023 Y2); operators may call them too:

- ``sales___line_pacing`` (READ): each line of the published PDS snapshot (the demo lines and the buys booked over
  AdCP) with its budget, its spend from its budget shards and its pace (``core.pds.pacing``).
- ``sales___propose`` (WRITE): records a proposal about one line in the ledger (chain ``sales/proposals``, kind
  ``proposal``) with the line's pacing at the time. It changes nothing: a human commits a proposal, in the line's
  configuration or its AdCP buy. v1 has no tool that commits one.

The Gateway does not say who called a tool (``functions.tools.dispatch``), so a proposal's entry names the tool as its
principal; the Gateway's interceptor records the agent's own entry for the same call, under the agent's principal and
the call's correlation id.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, Field

from fast_channels.core.ledger import Json, LedgerKind
from fast_channels.core.pds.book import LINE_ID_PATTERN, Line
from fast_channels.core.pds.delivery import BUDGET_SHARDS, budget_key
from fast_channels.core.pds.pacing import Pacing, pacing
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot
from fast_channels.functions import aws
from fast_channels.functions.ops.ledger import append, entry
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools.registry import Access, ToolContext, ToolError, tool

PROPOSALS_CHAIN: Final = "sales/proposals"
PRINCIPAL: Final = "tool:sales___propose"


class LinePace(BaseModel):
    """One line's pacing (``core.pds.pacing``)."""

    line: str
    advertiser: str
    budget: str = Field(description="US dollars (simulated).")
    spent: str | None = Field(description="US dollars reserved or delivered; null when its budget shards are unread.")
    starts: str | None = Field(description="Start of its flight (UTC); null: no flight.")
    ends: str | None = Field(description="End of its flight (UTC, exclusive); null: no flight.")
    elapsed_percent: int | None = Field(description="How much of its flight is gone.")
    spent_percent: int | None = Field(description="How much of its budget is spent.")
    pace: str | None = Field(description="spent share / elapsed share: 1.00 spends evenly to the end of its flight.")
    state: str = Field(
        description="on_pace, overpacing (pace above 1.2), underpacing (below 0.8), settling (under 5 percent of "
        "the flight gone), not_started, ended, no_flight (serves until its budget is spent) or unread."
    )


def line_pace(found: Pacing) -> LinePace:
    def iso(at: datetime | None) -> str | None:
        return None if at is None else f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"

    def percent(share: float | None) -> int | None:
        return None if share is None else round(share * 100)

    return LinePace(
        line=found.line,
        advertiser=found.advertiser,
        budget=f"{found.budget:.2f}",
        spent=None if found.spent is None else f"{found.spent:.2f}",
        starts=iso(found.starts),
        ends=iso(found.ends),
        elapsed_percent=percent(found.elapsed),
        spent_percent=percent(found.spent_share),
        pace=None if found.pace is None else f"{found.pace:.2f}",
        state=found.state.value,
    )


def published_lines() -> tuple[str, tuple[Line, ...]]:
    """The published snapshot's version and its own-book lines."""
    config = settings()
    config.require("pds_config_bucket")
    try:
        body = aws.client("s3").get_object(Bucket=config.pds_config_bucket, Key=SNAPSHOT_KEY)["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            raise ToolError("NOT_FOUND", "no PDS snapshot is published yet", hint="Try again in an hour.") from None
        raise
    snapshot = Snapshot.model_validate_json(body)
    return snapshot.version, snapshot.lines


def remaining(line: str) -> Decimal | None:
    """What the line's budget shards have left in total; None when any shard is missing or unreadable."""
    config = settings()
    config.require("pds_table")
    table = aws.table(config.pds_table)
    total = Decimal(0)
    try:
        for shard in range(BUDGET_SHARDS):
            item = table.get_item(Key=budget_key(line, shard), ConsistentRead=True).get("Item")
            if item is None or "remaining" not in item:
                return None
            total += Decimal(str(item["remaining"]))
    except ClientError, BotoCoreError:
        return None
    return total


def pacings(at: datetime, *, only: str | None = None) -> tuple[str, list[Pacing]]:
    """The snapshot's version and each of its lines' pacing (``only``: that line)."""
    version, lines = published_lines()
    chosen = [line for line in lines if only is None or line.id == only]
    if only is not None and not chosen:
        raise ToolError(
            "NOT_FOUND", f"{only} is not a line of the own book", hint="Call sales___line_pacing for the lines."
        )
    return version, [pacing(line, remaining(line.id), at) for line in chosen]


class LinePacingInput(BaseModel):
    line: str | None = Field(None, pattern=LINE_ID_PATTERN, description="One line id; omit it for every line.")


class LinePacingOutput(BaseModel):
    generated_at: str
    snapshot: str = Field(description="The PDS snapshot the lines come from.")
    lines: list[LinePace]


@tool("sales", access=Access.READ)
def line_pacing(payload: LinePacingInput, ctx: ToolContext) -> LinePacingOutput:
    """How the own book's lines are pacing: per line its budget, spend (reserved and delivered, from its budget
    shards), flight and pace (the share of the budget spent over the share of the flight gone; 1.00 spends evenly).
    Overpacing lines spend their budget before their flight ends; underpacing lines leave it unspent."""
    at = datetime.now(UTC)
    version, found = pacings(at, only=payload.line)
    return LinePacingOutput(
        generated_at=f"{at:%Y-%m-%dT%H:%M:%SZ}", snapshot=version, lines=[line_pace(p) for p in found]
    )


class ProposalAction(StrEnum):
    PAUSE = "pause"
    END_FLIGHT = "end_flight"
    EXTEND_FLIGHT = "extend_flight"
    CHANGE_BUDGET = "change_budget"
    CAP_PER_SESSION = "cap_per_session"


class ProposeInput(BaseModel):
    line: str = Field(pattern=LINE_ID_PATTERN, description="The line id, from sales___line_pacing.")
    action: ProposalAction = Field(description="What a human should change.")
    value: str | None = Field(
        None,
        max_length=64,
        description="The new value: a UTC ISO-8601 time for a flight, US dollars for a budget, impressions per "
        "session for a cap; omit it for pause.",
    )
    rationale: str = Field(min_length=1, max_length=1000, description="Why, from the line's pacing.")


class ProposeOutput(BaseModel):
    proposal_id: str
    line: str
    action: str
    value: str | None
    recorded_at: str
    chain: str = Field(description="The ledger chain that holds the proposal.")
    pacing: LinePace = Field(description="The line's pacing when it was proposed.")
    note: str


@tool("sales", access=Access.WRITE)
def propose(payload: ProposeInput, ctx: ToolContext) -> ProposeOutput:
    """Propose a change to one own-book line, for a human to decide: pause it, end or extend its flight, change its
    budget or cap it per session. Records the proposal, with the line's current pacing, in the ledger; changes
    nothing on air or in the book."""
    at = datetime.now(UTC)
    _, [found] = pacings(at, only=payload.line)
    pace = line_pace(found)
    detail: dict[str, Json] = {
        "line": payload.line,
        "action": payload.action.value,
        "value": payload.value,
        "rationale": payload.rationale,
        "pacing": json.loads(pace.model_dump_json()),
    }
    config = settings()
    config.require("ledger_table")
    recorded = append(
        entry(
            PROPOSALS_CHAIN,
            LedgerKind.PROPOSAL,
            principal=PRINCIPAL,
            action=f"sales.propose.{payload.action.value}",
            subject=payload.line,
            correlation_id=ctx.correlation_id,
            detail=detail,
        ),
        table=config.ledger_table,
    )
    return ProposeOutput(
        proposal_id=recorded.record_id,
        line=payload.line,
        action=payload.action.value,
        value=payload.value,
        recorded_at=f"{recorded.at:%Y-%m-%dT%H:%M:%SZ}",
        chain=PROPOSALS_CHAIN,
        pacing=pace,
        note="Recorded for a human to decide; nothing changed.",
    )


handler = dispatch.handler("sales")
