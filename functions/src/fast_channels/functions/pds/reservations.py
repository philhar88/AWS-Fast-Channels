"""Budgets, reservations, deliveries and releases of the own book's lines (ADR-0022, ``core.pds.delivery``).

Each change of money is one ``TransactWriteItems`` over the PDS table and the ledger, so a line's budget shard, the
reservation and its money event change together or not at all:

- :func:`set_budgets` (the snapshot publisher) gives each line's shards their share of its budget;
- :func:`reserve` (``/select``) takes the price of a break's line ads from a shard (the caller picks one with
  ``budget.Budgets.shard``), counts them against the line's per-session cap and writes their reservations; a shard's
  ``remaining`` never goes below zero;
- :func:`deliver` (the delivery function) marks a reservation DELIVERED when MediaTailor fires our tracker;
- :func:`release` (the sweeper) gives back the price of a reservation that saw no impression in two hours.

Amounts are DynamoDB numbers (exact decimals); the ledger holds them as decimal strings.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

from fast_channels.core.ledger import LedgerKind, entry_item
from fast_channels.core.pds.delivery import (
    CURRENCY,
    PENDING,
    PRINCIPAL,
    Action,
    Credit,
    State,
    budget_key,
    cap_key,
    chain_id,
    money,
    reservation_key,
    shard_caps,
)
from fast_channels.functions.ops.ledger import entry

if TYPE_CHECKING:
    from fast_channels.core.pds.book import Line

logger = Logger(child=True)

ATTEMPTS: Final = 2
"""Transactions per reservation: a cancelled one is retried once without the ads it refused."""
CLEANUP: Final = timedelta(days=35)
"""When DynamoDB may delete an item (``ttl``): cleanup only, after reconciliation; nothing reads it as a state."""
CANCELLED: Final = "TransactionCanceledException"
FAILED_CONDITION: Final = "ConditionalCheckFailed"


class Refusal(StrEnum):
    """Why a line's ads were not reserved."""

    BUDGET = "budget"
    """The shard does not have their price left."""
    CAP = "cap"
    """The session has reached the line's cap."""
    CONFLICT = "conflict"
    """Concurrent transactions kept cancelling this one."""


@dataclass(frozen=True, slots=True)
class Hold:
    """One line's ads in one break, whose price comes from one of its shards."""

    line: Line
    crids: tuple[str, ...]
    shard: int

    @property
    def amount(self) -> Decimal:
        return self.line.insertion * len(self.crids)


@dataclass(frozen=True, slots=True)
class Reservation:
    """What :func:`reserve` did: crids reserved now, crids this break had reserved already, refused crids."""

    held: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()
    refused: Mapping[str, Refusal] = field(default_factory=dict)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: Decimal | int) -> dict[str, str]:
    return {"N": str(value)}


def item_key(key: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """A key (``core.pds.delivery``) as DynamoDB's low-level API takes it."""
    return {name: _s(value) for name, value in key.items()}


def _iso(at: datetime) -> str:
    """UTC, so that times compare as strings (the pending index sorts by them)."""
    return at.astimezone(UTC).isoformat(timespec="milliseconds")


def _epoch(at: datetime) -> int:
    return int(at.timestamp())


def _ledger(
    table: str, line: str, action: Action, *, subject: str, at: datetime, decision: str | None, **detail: Any
) -> dict[str, Any]:
    """The ledger put of one money event of ``line`` (its chain ``pds/line/<id>``), correlated by the break."""
    record = entry(
        chain_id(line),
        LedgerKind.MONEY,
        principal=PRINCIPAL,
        action=action.value,
        subject=subject,
        correlation_id=decision,
        detail={"line": line, "currency": CURRENCY, **detail},
        at=at,
    )
    return {
        "Put": {
            "TableName": table,
            "Item": {name: _s(value) for name, value in entry_item(record).items()},
            "ConditionExpression": "attribute_not_exists(pk)",
        }
    }


def _token(actions: Sequence[Mapping[str, Any]]) -> str:
    """The transaction's idempotency token: identical actions (a retry by the SDK) carry the same one."""
    return hashlib.sha256(json.dumps(actions, sort_keys=True).encode()).hexdigest()[:32]


def _cancellation(error: ClientError) -> list[str] | None:
    """Each action's cancellation code (``None`` where it passed), or None when the error is not a cancellation."""
    if error.response.get("Error", {}).get("Code") != CANCELLED:
        return None
    return [str(reason.get("Code") or "None") for reason in error.response.get("CancellationReasons", [])]


def set_budgets(
    client: Any, *, table: str, ledger_table: str, lines: Sequence[Line], now: datetime
) -> dict[str, list[int]]:
    """Give each shard of ``lines`` its share of the line's budget (``shard_caps``) and return the shards changed.

    A new shard starts with its share; a changed budget moves ``remaining`` by the change, so what is reserved stays
    reserved (a budget cut below it leaves the line exhausted). Each change is a ledger money event.
    """
    changed: dict[str, list[int]] = {}
    for line in lines:
        for shard, cap in enumerate(shard_caps(line.budget)):
            key = item_key(budget_key(line.id, shard))
            item = client.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
            old = Decimal(item["cap"]["N"]) if item is not None and "cap" in item else None
            if old == cap:
                continue
            if old is None:
                write: dict[str, Any] = {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            **key,
                            "line": _s(line.id),
                            "cap": _n(cap),
                            "remaining": _n(cap),
                            "delivered": _n(0),
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            else:
                write = {
                    "Update": {
                        "TableName": table,
                        "Key": key,
                        "UpdateExpression": "SET cap = :cap, remaining = remaining + :change",
                        "ConditionExpression": "cap = :old",
                        "ExpressionAttributeValues": {":cap": _n(cap), ":old": _n(old), ":change": _n(cap - old)},
                    }
                }
            subject = f"budget/{line.id}/{shard}"
            ledger = _ledger(
                ledger_table,
                line.id,
                Action.BUDGET,
                subject=subject,
                at=now,
                decision=None,
                shard=shard,
                amount=money(cap),
            )
            try:
                client.transact_write_items(TransactItems=[write, ledger])
            except ClientError as error:
                if ((_cancellation(error) or ["None"])[0]) != FAILED_CONDITION:
                    raise
                # Another run (the deploy's, the hourly one) changed the shard first: the next run sees it.
                logger.info("budget shard changed meanwhile", extra={"line": line.id, "shard": shard})
                continue
            changed.setdefault(line.id, []).append(shard)
    return changed


def _reserve_actions(
    holds: Sequence[Hold], *, table: str, ledger_table: str, decision: str, sid: str, now: datetime
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str | None]]]:
    """The transaction for ``holds``, and each action's owner: (line, role, crid)."""
    actions: list[dict[str, Any]] = []
    owners: list[tuple[str, str, str | None]] = []
    for hold in holds:
        line = hold.line
        actions.append(
            {
                "Update": {
                    "TableName": table,
                    "Key": item_key(budget_key(line.id, hold.shard)),
                    "UpdateExpression": "SET remaining = remaining - :amount",
                    "ConditionExpression": "remaining >= :amount",
                    "ExpressionAttributeValues": {":amount": _n(hold.amount)},
                }
            }
        )
        owners.append((line.id, "budget", None))
        if line.per_session is not None:
            actions.append(
                {
                    "Update": {
                        "TableName": table,
                        "Key": item_key(cap_key(sid, line.id)),
                        "UpdateExpression": "ADD n :count SET #ttl = :ttl",
                        "ConditionExpression": "attribute_not_exists(n) OR n <= :most",
                        "ExpressionAttributeNames": {"#ttl": "ttl"},
                        "ExpressionAttributeValues": {
                            ":count": _n(len(hold.crids)),
                            ":most": _n(line.per_session - len(hold.crids)),
                            ":ttl": _n(_epoch(now + CLEANUP)),
                        },
                    }
                }
            )
            owners.append((line.id, "cap", None))
        for crid in hold.crids:
            actions.append(
                {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            **item_key(reservation_key(decision, crid)),
                            "state": _s(State.RESERVED),
                            "line": _s(line.id),
                            "shard": _n(hold.shard),
                            "amount": _n(line.insertion),
                            "sid": _s(sid),
                            "reserved_at": _s(_iso(now)),
                            "pending": _s(PENDING),
                            "pending_at": _s(_iso(now)),
                            "ttl": _n(_epoch(now + CLEANUP)),
                        },
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                }
            )
            owners.append((line.id, "reservation", crid))
            actions.append(
                _ledger(
                    ledger_table,
                    line.id,
                    Action.RESERVE,
                    subject=f"{decision}/{crid}",
                    at=now,
                    decision=decision,
                    crid=crid,
                    shard=hold.shard,
                    amount=money(line.insertion),
                )
            )
            owners.append((line.id, "ledger", crid))
    return actions, owners


def reserve(
    client: Any,
    *,
    table: str,
    ledger_table: str,
    decision: str,
    sid: str,
    holds: Sequence[Hold],
    now: datetime,
) -> Reservation:
    """Reserve the line ads of one break: all of a line's ads or none of them.

    A transaction that DynamoDB cancels is retried once without the lines it refused (no budget left on the shard,
    the session's cap reached) and without the ads this break had already reserved (a repeated ``/select``).

    Raises:
        ValueError: two holds are for one line.
    """
    if len({hold.line.id for hold in holds}) != len(holds):
        raise ValueError("one hold per line")
    held: list[str] = []
    existing: list[str] = []
    refused: dict[str, Refusal] = {}
    pending = [hold for hold in holds if hold.crids]
    for hold in pending:
        if hold.line.per_session is not None and len(hold.crids) > hold.line.per_session:
            refused.update(dict.fromkeys(hold.crids, Refusal.CAP))
    pending = [hold for hold in pending if hold.crids[0] not in refused]
    for _ in range(ATTEMPTS):
        if not pending:
            break
        actions, owners = _reserve_actions(
            pending, table=table, ledger_table=ledger_table, decision=decision, sid=sid, now=now
        )
        try:
            client.transact_write_items(TransactItems=actions, ClientRequestToken=_token(actions))
        except ClientError as error:
            if (codes := _cancellation(error)) is None:
                raise
            pending = _after_cancellation(pending, owners, codes, existing, refused)
            continue
        held.extend(crid for hold in pending for crid in hold.crids)
        pending = []
    for hold in pending:
        refused.update(dict.fromkeys(hold.crids, Refusal.CONFLICT))
    return Reservation(held=tuple(held), existing=tuple(existing), refused=refused)


def _after_cancellation(
    pending: Sequence[Hold],
    owners: Sequence[tuple[str, str, str | None]],
    codes: Sequence[str],
    existing: list[str],
    refused: dict[str, Refusal],
) -> list[Hold]:
    """The holds to try again after a cancelled transaction; records the existing and refused ads.

    Ads the break had reserved already come first: the budget and cap conditions counted them too, so a line with
    one is retried with the others alone. Otherwise a failed budget or cap condition refuses the line's ads, and a
    line cancelled only by another one's failure (or by a conflicting transaction) is retried as it was.
    """
    failed = {owner for owner, code in zip(owners, codes, strict=False) if code == FAILED_CONDITION}
    retry: list[Hold] = []
    for hold in pending:
        line = hold.line.id
        if reserved := [crid for crid in hold.crids if (line, "reservation", crid) in failed]:
            existing.extend(reserved)
            if left := tuple(crid for crid in hold.crids if crid not in reserved):
                retry.append(Hold(line=hold.line, crids=left, shard=hold.shard))
        elif (line, "budget", None) in failed:
            refused.update(dict.fromkeys(hold.crids, Refusal.BUDGET))
        elif (line, "cap", None) in failed:
            refused.update(dict.fromkeys(hold.crids, Refusal.CAP))
        else:
            retry.append(hold)
    return retry


def _transition(
    item: Mapping[str, Any], to: State, *, table: str, ledger_table: str, decision: str, crid: str, at: datetime
) -> list[dict[str, Any]]:
    """The transaction that moves one reservation from its state to ``to``, with the shard and the ledger."""
    was, line = item["state"]["S"], item["line"]["S"]
    shard, amount = int(item["shard"]["N"]), Decimal(item["amount"]["N"])
    stamp = {State.DELIVERED: "delivered_at", State.RELEASED: "released_at"}[to]
    late = was == State.RELEASED
    budget: dict[str, Any] = {
        State.DELIVERED: {
            "UpdateExpression": "SET remaining = remaining - :amount ADD delivered :amount"
            if late
            else "ADD delivered :amount"
        },
        State.RELEASED: {"UpdateExpression": "SET remaining = remaining + :amount"},
    }[to]
    action = Action.DELIVER if to is State.DELIVERED else Action.RELEASE
    return [
        {
            "Update": {
                "TableName": table,
                "Key": item_key(reservation_key(decision, crid)),
                "UpdateExpression": f"SET #state = :to, {stamp} = :at REMOVE pending, pending_at",
                "ConditionExpression": "#state = :was",
                "ExpressionAttributeNames": {"#state": "state"},
                "ExpressionAttributeValues": {":to": _s(to), ":was": _s(was), ":at": _s(_iso(at))},
            }
        },
        {
            "Update": {
                "TableName": table,
                "Key": item_key(budget_key(line, shard)),
                "ExpressionAttributeValues": {":amount": _n(amount)},
                **budget,
            }
        },
        _ledger(
            ledger_table,
            line,
            action,
            subject=f"{decision}/{crid}",
            at=at,
            decision=decision,
            crid=crid,
            shard=shard,
            amount=money(amount),
            **({"late": True} if late else {}),
        ),
    ]


def _reservation(client: Any, table: str, decision: str, crid: str) -> dict[str, Any] | None:
    item: dict[str, Any] | None = client.get_item(
        TableName=table, Key=item_key(reservation_key(decision, crid)), ConsistentRead=True
    ).get("Item")
    return item


def deliver(
    client: Any, *, table: str, ledger_table: str, decision: str, crid: str, at: datetime
) -> tuple[Credit, dict[str, Any] | None]:
    """Credit one impression of ``crid`` in break ``decision`` to its reservation, if it has one; returns what it did
    and the reservation item.

    Idempotent: a repeated impression finds the reservation DELIVERED (:attr:`Credit.REPEAT`). A RELEASED one is
    delivered late: its price is taken from the shard again, even if that leaves the shard below zero, because the
    impression happened.
    """
    for _ in range(ATTEMPTS + 1):
        item = _reservation(client, table, decision, crid)
        if item is None:
            return Credit.UNRESERVED, None
        state = State(item["state"]["S"])
        if state in {State.DELIVERED, State.RECONCILED}:
            return Credit.REPEAT, item
        actions = _transition(
            item, State.DELIVERED, table=table, ledger_table=ledger_table, decision=decision, crid=crid, at=at
        )
        try:
            client.transact_write_items(TransactItems=actions, ClientRequestToken=_token(actions))
        except ClientError as error:
            if _cancellation(error) is None:
                raise
            continue  # its state changed meanwhile (the sweeper released it): read it again
        return (Credit.LATE if state is State.RELEASED else Credit.DELIVERED), item
    raise RuntimeError(f"reservation {decision}/{crid} kept changing")


def release(
    client: Any, *, table: str, ledger_table: str, decision: str, crid: str, now: datetime
) -> dict[str, Any] | None:
    """Release one RESERVED reservation (no impression in time): its price goes back to its shard. Returns the item,
    or None when it is no longer RESERVED (delivered meanwhile, or released by another run)."""
    item = _reservation(client, table, decision, crid)
    if item is None or item["state"]["S"] != State.RESERVED:
        return None
    actions = _transition(
        item, State.RELEASED, table=table, ledger_table=ledger_table, decision=decision, crid=crid, at=now
    )
    try:
        client.transact_write_items(TransactItems=actions, ClientRequestToken=_token(actions))
    except ClientError as error:
        if _cancellation(error) is None:
            raise
        return None
    return item
