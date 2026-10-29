"""Budgets, reservations, deliveries and releases against DynamoDB Local (M2-03, ADR-0022).

The acceptance: under concurrent reservations a line's liability never exceeds its budget; reservations are
idempotent; every change of money is in the ledger.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from fast_channels.core.ledger import LedgerKind, entry_from_item
from fast_channels.core.pds.book import Line
from fast_channels.core.pds.delivery import (
    BUDGET_SHARDS,
    PENDING,
    Credit,
    State,
    budget_key,
    cap_key,
    home_shard,
    reservation_key,
    shard_caps,
)
from fast_channels.functions.pds.reservations import Hold, Refusal, deliver, release, reserve, set_budgets

if TYPE_CHECKING:
    from conftest import Tables

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
SID = "0a1e6454-90e6-423a-80c6-5e02eff31cdc"
DIRECT1 = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=Decimal("8.00"),
    budget=Decimal(100),
    tier0=True,
)
DIRECT2 = Line(
    id="direct-2",
    advertiser="Direct Advertiser 2",
    adomain="direct2.example",
    creatives=("direct2", "direct2b"),
    cpm=Decimal("6.00"),
    budget=Decimal(1),
    per_session=2,
)


def decision(evid: int, sid: str = SID) -> str:
    return f"{sid}.{evid}"


def shards(db: Tables, line: Line) -> list[dict[str, Decimal]]:
    found = [db.item(budget_key(line.id, n)) for n in range(BUDGET_SHARDS)]
    return [{name: Decimal(item[name]["N"]) for name in ("cap", "remaining", "delivered")} for item in found if item]


def remaining(db: Tables, line: Line) -> Decimal:
    return sum((shard["remaining"] for shard in shards(db, line)), Decimal(0))


def money_events(db: Tables) -> list[tuple[str, str, dict[str, Any]]]:
    entries = [entry_from_item({k: v["S"] for k, v in item.items()}) for item in db.ledger_entries()]
    assert all(e.kind is LedgerKind.MONEY and e.principal == "pds" for e in entries)
    return sorted(((e.action, e.subject, e.detail) for e in entries), key=lambda e: (e[0], e[1], json.dumps(e[2])))


def hold(line: Line, *crids: str, evid: int = 1) -> Hold:
    return Hold(line=line, crids=crids or line.creatives[:1], shard=home_shard(decision(evid)))


def test_budgets_split_into_shards_and_change_by_the_difference(dynamodb: Tables) -> None:
    db = dynamodb
    assert set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1, DIRECT2], now=NOW) == {
        "direct-1": [0, 1, 2, 3],
        "direct-2": [0, 1, 2, 3],
    }
    assert [s["cap"] for s in shards(db, DIRECT2)] == list(shard_caps(Decimal(1)))
    assert remaining(db, DIRECT1) == Decimal(100)
    assert set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1, DIRECT2], now=NOW) == {}

    reserve(
        db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW
    )
    raised = DIRECT1.model_copy(update={"budget": Decimal(200)})
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[raised], now=NOW)

    assert remaining(db, DIRECT1) == Decimal(200) - DIRECT1.insertion, "what is reserved stays reserved"
    budget_events = [e for e in money_events(db) if e[0] == "money.budget"]
    assert len(budget_events) == 3 * BUDGET_SHARDS
    assert budget_events[0][2] == {"line": "direct-1", "currency": "USD", "shard": 0, "amount": "25.000000"}


def test_a_reservation_takes_the_price_and_records_it(dynamodb: Tables) -> None:
    db = dynamodb
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1], now=NOW)

    done = reserve(
        db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW
    )

    assert (done.held, done.existing, dict(done.refused)) == (("direct1",), (), {})
    item = db.item(reservation_key(decision(1), "direct1"))
    assert item is not None
    assert (item["state"]["S"], item["line"]["S"], Decimal(item["amount"]["N"])) == (
        "RESERVED",
        "direct-1",
        Decimal("0.008"),
    )
    assert (item["pending"]["S"], item["pending_at"]["S"]) == (PENDING, "2026-10-29T15:00:00.000+00:00")
    assert remaining(db, DIRECT1) == Decimal(100) - Decimal("0.008")
    [reserved] = [e for e in money_events(db) if e[0] == "money.reserve"]
    assert reserved[1:] == (
        f"{decision(1)}/direct1",
        {
            "line": "direct-1",
            "currency": "USD",
            "crid": "direct1",
            "shard": home_shard(decision(1)),
            "amount": "0.008000",
        },
    )


def test_reserving_a_break_again_reserves_nothing_more(dynamodb: Tables) -> None:
    db = dynamodb
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1, DIRECT2], now=NOW)
    holds = [hold(DIRECT1), hold(DIRECT2, "direct2")]
    first = reserve(
        db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), sid=SID, holds=holds, now=NOW
    )

    again = reserve(
        db.client,
        table=db.pds,
        ledger_table=db.ledger,
        decision=decision(1),
        sid=SID,
        holds=[hold(DIRECT1), hold(DIRECT2, "direct2", "direct2b")],
        now=NOW + timedelta(seconds=1),
    )

    assert sorted(first.held) == ["direct1", "direct2"]
    assert (again.held, sorted(again.existing)) == (("direct2b",), ["direct1", "direct2"])
    assert remaining(db, DIRECT1) == Decimal(100) - DIRECT1.insertion
    assert len([e for e in money_events(db) if e[0] == "money.reserve"]) == 3
    cap = db.item(cap_key(SID, "direct-2"))
    assert cap is not None and cap["n"]["N"] == "2", "the retry counted only the new ad"


def test_a_line_without_budget_left_is_refused_and_the_others_are_held(dynamodb: Tables) -> None:
    db = dynamodb
    poor = DIRECT2.model_copy(update={"budget": Decimal("0.02"), "per_session": None})
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1, poor], now=NOW)

    done = reserve(
        db.client,
        table=db.pds,
        ledger_table=db.ledger,
        decision=decision(1),
        sid=SID,
        holds=[hold(DIRECT1), hold(poor, "direct2")],
        now=NOW,
    )

    assert (done.held, dict(done.refused)) == (("direct1",), {"direct2": Refusal.BUDGET})
    assert db.item(reservation_key(decision(1), "direct2")) is None
    assert remaining(db, poor) == Decimal("0.02")
    assert [e[1] for e in money_events(db) if e[0] == "money.reserve"] == [f"{decision(1)}/direct1"]


def test_the_per_session_cap_holds_across_breaks(dynamodb: Tables) -> None:
    db = dynamodb
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT2], now=NOW)

    def ask(evid: int, *crids: str, sid: str = SID) -> Any:
        return reserve(
            db.client,
            table=db.pds,
            ledger_table=db.ledger,
            decision=decision(evid, sid),
            sid=sid,
            holds=[hold(DIRECT2, *crids, evid=evid)],
            now=NOW,
        )

    assert ask(1, "direct2").held == ("direct2",)
    assert dict(ask(2, "direct2", "direct2b").refused) == dict.fromkeys(("direct2", "direct2b"), Refusal.CAP)
    assert ask(3, "direct2b").held == ("direct2b",)
    assert dict(ask(4, "direct2").refused) == {"direct2": Refusal.CAP}
    assert ask(5, "direct2", sid="another-session").held == ("direct2",)
    assert dict(ask(6, "direct2", "direct2b", "direct2").refused) == {
        "direct2": Refusal.CAP,
        "direct2b": Refusal.CAP,
    }, "more ads than the cap are refused without a transaction"


def test_concurrent_reservations_never_exceed_the_budget(dynamodb: Tables) -> None:
    """300 breaks race for a $1 line at $6 CPM: at most 41 impressions fit each of its four $0.25 shards."""
    db = dynamodb
    line = DIRECT2.model_copy(update={"per_session": None})
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[line], now=NOW)
    breaks = range(300)

    def ask(evid: int) -> Any:
        return reserve(
            db.client,
            table=db.pds,
            ledger_table=db.ledger,
            decision=decision(evid),
            sid=SID,
            holds=[hold(line, "direct2", evid=evid)],
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(ask, breaks))

    held = [evid for evid, r in zip(breaks, results, strict=True) if r.held]
    refused = {reason for r in results for reason in r.refused.values()}
    capacity = BUDGET_SHARDS * int(Decimal("0.25") / line.insertion)
    liability = sum(
        (Decimal(db.item(reservation_key(decision(e), "direct2"))["amount"]["N"]) for e in held), Decimal(0)
    )
    assert 0 < len(held) <= capacity == 164
    assert refused <= {Refusal.BUDGET, Refusal.CONFLICT}
    assert liability == line.insertion * len(held) <= line.budget
    assert remaining(db, line) == line.budget - liability
    assert all(shard["remaining"] >= 0 for shard in shards(db, line))
    assert len([e for e in money_events(db) if e[0] == "money.reserve"]) == len(held)
    by_shard = {n: sum(1 for e in held if home_shard(decision(e)) == n) for n in range(BUDGET_SHARDS)}
    assert all(count <= 41 for count in by_shard.values())


def test_an_impression_delivers_its_reservation_once(dynamodb: Tables) -> None:
    db = dynamodb
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1], now=NOW)
    reserve(
        db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW
    )
    at = NOW + timedelta(seconds=30)

    credit, item = deliver(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="direct1", at=at)
    repeat, _ = deliver(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="direct1", at=at)
    none, missing = deliver(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="house1", at=at)

    assert (credit, repeat, none, missing) == (Credit.DELIVERED, Credit.REPEAT, Credit.UNRESERVED, None)
    assert item is not None and item["line"]["S"] == "direct-1"
    stored = db.item(reservation_key(decision(1), "direct1"))
    assert stored is not None
    assert stored["state"]["S"] == State.DELIVERED and "pending" not in stored and "pending_at" not in stored
    assert sum(s["delivered"] for s in shards(db, DIRECT1)) == DIRECT1.insertion
    assert remaining(db, DIRECT1) == Decimal(100) - DIRECT1.insertion, "a delivered price stays taken"
    assert [e[0] for e in money_events(db)].count("money.deliver") == 1


def test_a_release_gives_the_price_back_and_a_late_impression_takes_it_again(dynamodb: Tables) -> None:
    db = dynamodb
    set_budgets(db.client, table=db.pds, ledger_table=db.ledger, lines=[DIRECT1], now=NOW)
    reserve(
        db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW
    )
    later = NOW + timedelta(hours=2, minutes=5)

    released = release(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="direct1", now=later)
    twice = release(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="direct1", now=later)

    assert released is not None and twice is None
    assert remaining(db, DIRECT1) == Decimal(100)
    stored = db.item(reservation_key(decision(1), "direct1"))
    assert stored is not None and stored["state"]["S"] == State.RELEASED and "pending" not in stored

    credit, _ = deliver(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(1), crid="direct1", at=later)

    assert credit is Credit.LATE
    assert remaining(db, DIRECT1) == Decimal(100) - DIRECT1.insertion
    actions = [(e[0], e[2].get("late")) for e in money_events(db) if e[0] != "money.budget"]
    assert sorted(actions, key=str) == sorted(
        [("money.reserve", None), ("money.release", None), ("money.deliver", True)], key=str
    )
    assert release(db.client, table=db.pds, ledger_table=db.ledger, decision=decision(9), crid="x", now=later) is None


def test_errors_other_than_a_cancellation_are_raised() -> None:
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    with Stubber(client) as stubber:
        stubber.add_client_error("transact_write_items", service_error_code="ValidationException")
        with pytest.raises(ClientError, match="ValidationException"):
            reserve(client, table="t", ledger_table="l", decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW)
    with pytest.raises(ValueError, match="one hold per line"):
        reserve(client, table="t", ledger_table="l", decision=decision(1), sid=SID, holds=[hold(DIRECT1)] * 2, now=NOW)
    assert (
        json.dumps(
            reserve(client, table="t", ledger_table="l", decision=decision(1), sid=SID, holds=[], now=NOW).refused
        )
        == "{}"
    )


def stubbed() -> Any:
    return boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")


def cancelled(stubber: Stubber, *codes: str) -> None:
    stubber.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        modeled_fields={"CancellationReasons": [{"Code": code} for code in codes]},
    )


RESERVED = {
    "pk": {"S": f"RES#{SID}.1"},
    "sk": {"S": "direct1"},
    "state": {"S": "RESERVED"},
    "line": {"S": "direct-1"},
    "shard": {"N": "1"},
    "amount": {"N": "0.008"},
}


def test_a_line_that_keeps_conflicting_is_refused_as_a_conflict() -> None:
    client = stubbed()
    with Stubber(client) as stubber:
        cancelled(stubber, "TransactionConflict", "None", "None")
        cancelled(stubber, "TransactionConflict", "None", "None")
        done = reserve(
            client, table="t", ledger_table="l", decision=decision(1), sid=SID, holds=[hold(DIRECT1)], now=NOW
        )

    assert (done.held, dict(done.refused)) == ((), {"direct1": Refusal.CONFLICT})


def test_a_delivery_reads_a_changed_reservation_again_and_gives_up_if_it_keeps_changing() -> None:
    client = stubbed()
    with Stubber(client) as stubber:
        stubber.add_response("get_item", {"Item": RESERVED})
        cancelled(stubber, "ConditionalCheckFailed", "None", "None")
        stubber.add_response("get_item", {"Item": {**RESERVED, "state": {"S": "RELEASED"}}})
        stubber.add_response("transact_write_items", {})
        credit, _ = deliver(client, table="t", ledger_table="l", decision=decision(1), crid="direct1", at=NOW)
    assert credit is Credit.LATE

    with Stubber(client) as stubber:
        for _ in range(3):
            stubber.add_response("get_item", {"Item": RESERVED})
            cancelled(stubber, "ConditionalCheckFailed", "None", "None")
        with pytest.raises(RuntimeError, match="kept changing"):
            deliver(client, table="t", ledger_table="l", decision=decision(1), crid="direct1", at=NOW)


@pytest.mark.parametrize("operation", ["deliver", "release"])
def test_transition_errors_other_than_a_cancellation_are_raised(operation: str) -> None:
    client = stubbed()
    with Stubber(client) as stubber:
        stubber.add_response("get_item", {"Item": RESERVED})
        stubber.add_client_error("transact_write_items", service_error_code="InternalServerError")
        with pytest.raises(ClientError, match="InternalServerError"):
            if operation == "deliver":
                deliver(client, table="t", ledger_table="l", decision=decision(1), crid="direct1", at=NOW)
            else:
                release(client, table="t", ledger_table="l", decision=decision(1), crid="direct1", now=NOW)


def test_a_release_that_loses_a_race_releases_nothing() -> None:
    client = stubbed()
    with Stubber(client) as stubber:
        stubber.add_response("get_item", {"Item": RESERVED})
        cancelled(stubber, "ConditionalCheckFailed", "None", "None")
        assert release(client, table="t", ledger_table="l", decision=decision(1), crid="direct1", now=NOW) is None


def test_a_budget_another_run_set_first_is_left_to_it() -> None:
    client = stubbed()
    line = DIRECT1.model_copy(update={"budget": Decimal(4)})
    with Stubber(client) as stubber:
        for shard in range(BUDGET_SHARDS):
            stubber.add_response("get_item", {})
            if shard == 0:
                cancelled(stubber, "ConditionalCheckFailed", "None")
            else:
                stubber.add_response("transact_write_items", {})
        assert set_budgets(client, table="t", ledger_table="l", lines=[line], now=NOW) == {"direct-1": [1, 2, 3]}
    with Stubber(client) as stubber:
        stubber.add_response("get_item", {})
        stubber.add_client_error("transact_write_items", service_error_code="AccessDeniedException")
        with pytest.raises(ClientError, match="AccessDenied"):
            set_budgets(client, table="t", ledger_table="l", lines=[line], now=NOW)
