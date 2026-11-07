"""``sales`` target: the own book's pacing from the published snapshot and the budget shards, and proposals that only
record."""

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from fast_channels.core.ledger import LedgerEntry, LedgerKind
from fast_channels.core.pds.book import Line
from fast_channels.core.pds.delivery import BUDGET_SHARDS
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, SnapshotBase, build
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import sales
from fast_channels.functions.tools.registry import Access, ToolContext, ToolError, load

NOW = datetime.now(UTC)
CTX = ToolContext(tool="sales___propose", correlation_id="agent-sales-1")


def line(id: str, *, flight: bool = True) -> Line:
    return Line(
        id=id,
        advertiser=f"Advertiser {id}",
        adomain=f"{id}.example",
        creatives=(f"c-{id}",),
        cpm=Decimal(8),
        budget=Decimal(100),
        starts=NOW - timedelta(days=1) if flight else None,
        ends=NOW + timedelta(days=9) if flight else None,
    )


@dataclass
class Bucket:
    body: bytes | None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        assert (Bucket, Key) == ("pds-config", SNAPSHOT_KEY)
        if self.body is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.body)}


@dataclass
class Shards:
    """The PDS table's budget shards: what each line has left in each shard."""

    remaining: dict[str, Decimal]

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool) -> dict[str, Any]:  # noqa: N803
        line_id = Key["pk"].removeprefix("BUDGET#")
        if line_id not in self.remaining:
            return {}
        return {"Item": {**Key, "remaining": self.remaining[line_id] / BUDGET_SHARDS}}


@dataclass
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def __call__(self, item: LedgerEntry, *, table: str | None = None) -> LedgerEntry:
        assert table == "ledger"
        self.entries.append(item)
        return item


@pytest.fixture
def book(monkeypatch: pytest.MonkeyPatch) -> Iterator[Ledger]:
    monkeypatch.setenv("PDS_CONFIG_BUCKET", "pds-config")
    monkeypatch.setenv("PDS_TABLE", "pds")
    monkeypatch.setenv("LEDGER_TABLE", "ledger")
    settings.cache_clear()
    snapshot = build(SnapshotBase(lines=(line("direct-1"), line("direct-2"), line("evergreen", flight=False))), now=NOW)
    shards = Shards({"direct-1": Decimal(40), "direct-2": Decimal(95), "evergreen": Decimal(50)})
    monkeypatch.setattr(aws, "client", lambda service: Bucket(snapshot.model_dump_json().encode()))
    monkeypatch.setattr(aws, "table", lambda name: shards)
    ledger = Ledger()
    monkeypatch.setattr(sales, "append", ledger)
    yield ledger
    settings.cache_clear()


def test_the_tools_are_a_read_and_a_record() -> None:
    tools = load("sales")

    assert {name: spec.access for name, spec in tools.items()} == {"line_pacing": Access.READ, "propose": Access.WRITE}


def test_line_pacing_reads_the_snapshots_lines_and_their_shards(book: Ledger) -> None:
    found = sales.line_pacing(sales.LinePacingInput(), CTX)

    paces = {pace.line: pace for pace in found.lines}
    assert (paces["direct-1"].state, paces["direct-1"].spent, paces["direct-1"].pace) == ("overpacing", "60.00", "6.00")
    assert (paces["direct-2"].state, paces["direct-2"].spent_percent) == ("underpacing", 5)
    assert (paces["evergreen"].state, paces["evergreen"].pace) == ("no_flight", None)
    assert paces["direct-1"].elapsed_percent == 10 and re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", found.snapshot)


def test_one_line_or_an_unknown_one(book: Ledger) -> None:
    [only] = sales.line_pacing(sales.LinePacingInput(line="direct-2"), CTX).lines

    assert only.line == "direct-2"
    with pytest.raises(ToolError, match="not a line of the own book"):
        sales.line_pacing(sales.LinePacingInput(line="nope"), CTX)


def test_a_proposal_is_recorded_with_the_lines_pacing_and_changes_nothing(book: Ledger) -> None:
    payload = sales.ProposeInput(
        line="direct-1", action=sales.ProposalAction.CHANGE_BUDGET, value="160", rationale="Spends 60% in 10%."
    )

    found = sales.propose(payload, CTX)

    [entry] = book.entries
    assert (entry.chain_id, entry.kind, entry.principal, entry.action, entry.subject) == (
        sales.PROPOSALS_CHAIN,
        LedgerKind.PROPOSAL,
        sales.PRINCIPAL,
        "sales.propose.change_budget",
        "direct-1",
    )
    assert entry.correlation_id == "agent-sales-1" and entry.detail["pacing"]["state"] == "overpacing"  # type: ignore[index, call-overload]
    assert found.proposal_id == entry.record_id and found.pacing.pace == "6.00" and "nothing changed" in found.note


def test_without_a_published_snapshot_the_tool_says_so(monkeypatch: pytest.MonkeyPatch, book: Ledger) -> None:
    monkeypatch.setattr(aws, "client", lambda service: Bucket(None))

    with pytest.raises(ToolError, match="no PDS snapshot is published yet"):
        sales.line_pacing(sales.LinePacingInput(), CTX)
