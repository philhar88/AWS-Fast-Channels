"""The ticket lookup on ``/policy`` (``fast_channels.pds.bindings``), against DynamoDB Local: its conditions are
DynamoDB's own, as the identity API's redeem runs them."""

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from dynamodb_local import local_client

from fast_channels.core.identity.binding import IDLE_MS, LOOKUP_FAILED, REFRESH_MS
from fast_channels.core.identity.ticket import ticket_row
from fast_channels.core.pds.models import PolicyRequest, Reason
from fast_channels.core.pds.snapshot import Snapshot, SnapshotBase, build, demo_demand
from fast_channels.pds import policy
from fast_channels.pds.bindings import Bindings

SID = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d"
OTHER_SID = "0d1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d"
SNAPSHOT = build(
    SnapshotBase(demand=demo_demand("fastm")),
    origin_facing=("130.176.0.0/18",),
    ip_ranges="1",
    now=datetime(2026, 10, 29, tzinfo=UTC),
)
PC = "fastm-demo-p-a"
TICKET = "AbCdEfGhIjKlMnOpQr_-12"
VID = "0123456789abcdef0123456789abcdef"
NOW = 1_800_000_000_000


class Clock:
    def __init__(self, now: int = NOW) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def table(dynamodb_endpoint: str) -> Iterator[tuple[Any, str]]:
    client = local_client(dynamodb_endpoint)
    name = f"identity-{uuid.uuid4().hex[:8]}"
    client.create_table(
        TableName=name,
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        BillingMode="PAY_PER_REQUEST",
    )
    yield client, name
    client.delete_table(TableName=name)


def mint(client: Any, table: str, **fields: Any) -> None:
    row = ticket_row(ticket=TICKET, pc=PC, cid="cid-1", now_ms=NOW - 30_000, vid=VID, persona="dana").item()
    row |= fields
    item = {k: {"N": str(v)} if isinstance(v, int) else {"S": str(v)} for k, v in row.items()}
    client.put_item(TableName=table, Item=item)


def stored(client: Any, table: str) -> dict[str, Any]:
    item: dict[str, Any] = client.get_item(TableName=table, Key={"pk": {"S": f"T#{TICKET}"}}, ConsistentRead=True)[
        "Item"
    ]
    return {k: next(iter(v.values())) for k, v in item.items()}


def question(**fields: Any) -> dict[str, Any]:
    """A ``/policy`` body of ``SID`` on ``PC`` with the ticket."""
    body: dict[str, Any] = {
        "v": 1,
        "t": TICKET,
        "sid": SID,
        "pc": PC,
        "fv": "r0001",
        "evid": "820461826",
        "upid": "tears-of-steel",
        "dur_ms": 60060,
        "ua": "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0)",
        "xff": "81.2.69.160, 130.176.12.7, 3.81.20.30, 130.176.40.9",
        "cfva": "3.81.20.30:46532",
        "ctx": "",
        "probe": False,
    }
    return body | fields


def request(**fields: Any) -> PolicyRequest:
    return PolicyRequest.model_validate(question(**fields))


def test_a_fresh_ticket_is_self_redeemed_then_bound_and_a_replay_refused(table: tuple[Any, str]) -> None:
    client, name = table
    mint(client, name)
    clock = Clock()
    found = Bindings(name, client, clock=clock)

    first = found.inputs(request())
    assert (first.identity, first.viewer.vid, first.viewer.persona) == (Reason.BOUND, VID, "dana")
    assert stored(client, name)["redeemedBy"] == SID, "the hook's redeem never came: the PDS bound it"
    clock.now += 1000
    assert found.inputs(request()).identity is Reason.BOUND
    replay = found.inputs(request(sid=OTHER_SID))
    assert (replay.identity, replay.viewer.vid) == (Reason.REPLAY, None)
    assert stored(client, name)["redeemedBy"] == SID


def test_the_lease_renews_last_seen_and_ends_after_two_idle_hours(table: tuple[Any, str]) -> None:
    client, name = table
    mint(client, name, redeemedBy=SID, bound_at=NOW - 60_000, last_seen=NOW - REFRESH_MS)
    found = Bindings(name, client, clock=Clock())
    assert found.inputs(request()).identity is Reason.BOUND
    assert int(stored(client, name)["last_seen"]) == NOW
    later = Bindings(name, client, clock=Clock(NOW + IDLE_MS))
    assert later.inputs(request()).identity is Reason.EXPIRED_OR_RACED


@pytest.mark.parametrize(
    ("fields", "change", "reason"),
    [
        ({}, {"pc": "fastm-demo-p-b"}, Reason.PC_MISMATCH),
        ({"redeem_by": NOW - 1}, {}, Reason.EXPIRED_OR_RACED),
    ],
    ids=["another configuration", "never redeemed in time"],
)
def test_a_ticket_that_cannot_bind_is_anonymous(
    table: tuple[Any, str], fields: dict[str, Any], change: dict[str, Any], reason: Reason
) -> None:
    client, name = table
    mint(client, name, **fields)
    inputs = Bindings(name, client, clock=Clock()).inputs(request(**change))
    assert (inputs.identity, inputs.viewer.vid) == (reason, None)
    assert "redeemedBy" not in stored(client, name)


def test_an_unknown_ticket_and_one_without_a_lookup(table: tuple[Any, str]) -> None:
    client, name = table
    found = Bindings(name, client, clock=Clock())
    assert found.inputs(request()).identity is Reason.UNKNOWN
    assert found.inputs(request(t="")).identity is Reason.MISSING
    assert found.inputs(request(t="short")).identity is Reason.INVALID


class _Failing:
    def get_item(self, **_: Any) -> Any:
        raise ConnectTimeoutError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")


class _Throttled:
    def get_item(self, **_: Any) -> Any:
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")


@pytest.mark.parametrize("client", [_Failing(), _Throttled()], ids=["timeout", "throttled"])
def test_a_failed_lookup_is_safe(client: Any) -> None:
    inputs = Bindings("t", client, clock=Clock()).inputs(request())
    assert inputs == LOOKUP_FAILED


class _Store:
    def get(self) -> Snapshot:
        return SNAPSHOT


def test_policy_records_the_bound_viewer(
    table: tuple[Any, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, name = table
    mint(client, name, redeem_by=time.time_ns() // 10**6 + 60_000)
    monkeypatch.setattr(policy, "store", _Store)
    monkeypatch.setattr(policy, "bindings", lambda: Bindings(name, client))

    def ask(**fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        caplog.clear()
        answer = policy.answer(json.dumps(question(**fields)).encode(), time.perf_counter())
        [record] = [vars(r) for r in caplog.records if getattr(r, "record", None) == "pds.policy"]
        return json.loads(answer.body), record

    answer, record = ask()
    assert (answer["mode"], answer["reason"]) == ("PSEUDONYMOUS", "bound")
    assert VID not in json.dumps(answer), "the vid never reaches Prebid Server"
    assert (record["mode"], record["reason"], record["vid"], record["cid"]) == ("PSEUDONYMOUS", "bound", VID, "cid-1")
    answer, record = ask(sid=OTHER_SID)
    assert (answer["mode"], answer["reason"], record["vid"], record["cid"]) == ("CONTEXTUAL", "replay", None, None)
