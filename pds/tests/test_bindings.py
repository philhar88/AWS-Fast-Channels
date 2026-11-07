"""The ticket lookup on ``/policy`` (``fast_channels.pds.bindings``), against DynamoDB Local: its conditions are
DynamoDB's own, as the identity API's redeem runs them; and the bound viewer's consent read beside it."""

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
from dynamodb_local import local_client

from fast_channels.core.identity import consent as consents
from fast_channels.core.identity.binding import IDLE_MS, LOOKUP_FAILED, REFRESH_MS
from fast_channels.core.identity.consent import MAX_AGE, UNKNOWN, ConsentChoice
from fast_channels.core.identity.ticket import ticket_row
from fast_channels.core.pds.models import Mode, PolicyRequest, Reason
from fast_channels.core.pds.policy import Consent, resolve
from fast_channels.core.pds.snapshot import Snapshot, SnapshotBase, build, demo_demand
from fast_channels.pds import policy
from fast_channels.pds.bindings import CONSENT_FRESH_S, CONSENT_STALE_S, Bindings

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


def consent(client: Any, table: str, *, at: int = NOW, **choice: Any) -> None:
    """``VID``'s consent row, recorded at ``at``."""
    row = consents.record(VID, ConsentChoice(**{"opt_out": False, **choice}), now_ms=at).item()
    item = {
        k: {"BOOL": v} if isinstance(v, bool) else {"N": str(v)} if isinstance(v, int) else {"S": str(v)}
        for k, v in row.items()
        if k != "gpp_sid"
    }
    if row.get("gpp_sid"):
        item["gpp_sid"] = {"L": [{"N": str(n)} for n in row["gpp_sid"]]}
    client.put_item(TableName=table, Item=item)


def mode(inputs: Any) -> tuple[Mode, Reason]:
    resolution = resolve(inputs.identity, viewer=inputs.viewer, consent=inputs.consent)
    return resolution.mode, resolution.reason


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
    consent(client, name, at=time.time_ns() // 10**6)
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


def test_a_signed_in_viewers_consent_decides_and_a_change_reaches_the_next_break_in_five_seconds(
    table: tuple[Any, str],
) -> None:
    client, name = table
    mint(client, name, redeemedBy=SID, bound_at=NOW - 60_000, last_seen=NOW - 60_000)
    clock = Clock()
    found = Bindings(name, client, clock=clock)
    assert mode(found.inputs(request())) == (Mode.SAFE, Reason.CONSENT_UNAVAILABLE), "no consent recorded"
    clock.now += int(CONSENT_FRESH_S * 1000)
    consent(client, name, vppa=True, gpp="DBABMA~CPXx", gpp_sid=[2])
    full = found.inputs(request())
    assert mode(full) == (Mode.FULL, Reason.BOUND)
    assert (full.viewer.vid, full.viewer.gpp, full.viewer.gpp_sid) == (VID, "DBABMA~CPXx", (2,))
    consent(client, name, opt_out=True)
    clock.now += 1000
    assert mode(found.inputs(request())) == (Mode.FULL, Reason.BOUND), "kept for five seconds"
    clock.now += int(CONSENT_FRESH_S * 1000)
    opted_out = found.inputs(request())
    assert mode(opted_out) == (Mode.CONTEXTUAL, Reason.OPTED_OUT)
    assert resolve(opted_out.identity, viewer=opted_out.viewer, consent=opted_out.consent).viewer.vid is None


def test_a_stale_or_malformed_consent_is_safe(table: tuple[Any, str]) -> None:
    client, name = table
    mint(client, name, redeemedBy=SID, bound_at=NOW - 60_000, last_seen=NOW - 60_000)
    consent(client, name, at=NOW - int(MAX_AGE.total_seconds() * 1000) - 1)
    assert mode(Bindings(name, client, clock=Clock()).inputs(request())) == (Mode.SAFE, Reason.CONSENT_UNAVAILABLE)
    client.put_item(TableName=name, Item={"pk": {"S": consents.key(VID)}, "opt_out": {"S": "maybe"}})
    assert Bindings(name, client, clock=Clock()).consent(VID) == UNKNOWN


class _ConsentFails:
    """DynamoDB Local, except that consent reads fail once ``failing`` is set."""

    def __init__(self, client: Any) -> None:
        self.client, self.failing = client, False

    def get_item(self, **request: Any) -> Any:
        if self.failing and request["Key"]["pk"]["S"].startswith(consents.KEY_PREFIX):
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem")
        return self.client.get_item(**request)

    def update_item(self, **request: Any) -> Any:
        return self.client.update_item(**request)


def test_a_failed_consent_read_keeps_the_last_answer_for_a_minute_then_is_safe(table: tuple[Any, str]) -> None:
    client, name = table
    mint(client, name, redeemedBy=SID, bound_at=NOW - 60_000, last_seen=NOW - 60_000)
    consent(client, name)
    failing = _ConsentFails(client)
    clock = Clock()
    found = Bindings(name, failing, clock=clock)
    assert found.consent(VID).consent is Consent.PSEUDONYMOUS
    failing.failing = True
    clock.now += int(CONSENT_STALE_S * 1000) - 1
    assert found.consent(VID).consent is Consent.PSEUDONYMOUS, "the store failed: the answer of a minute ago holds"
    clock.now += 1
    assert found.consent(VID) == UNKNOWN, "older than a minute: SAFE"


class _Counting:
    def __init__(self, client: Any) -> None:
        self.client, self.keys = client, list[str]()

    def get_item(self, **request: Any) -> Any:
        self.keys.append(request["Key"]["pk"]["S"])
        return self.client.get_item(**request)


def test_a_guest_ticket_is_contextual_with_its_gpp_string_and_reads_no_consent(table: tuple[Any, str]) -> None:
    client, name = table
    row = ticket_row(
        ticket=TICKET, pc=PC, cid="cid-2", now_ms=NOW - 30_000, pm=consents.guest_mode(gpc=False), gpp="DBAA"
    ).item()
    row |= {"redeemedBy": SID, "bound_at": NOW - 60_000, "last_seen": NOW - 60_000}
    client.put_item(
        TableName=name, Item={k: {"N": str(v)} if isinstance(v, int) else {"S": str(v)} for k, v in row.items()}
    )
    counting = _Counting(client)
    inputs = Bindings(name, counting, clock=Clock()).inputs(request())
    assert mode(inputs) == (Mode.CONTEXTUAL, Reason.BOUND)
    assert (inputs.viewer.vid, inputs.viewer.cid, inputs.viewer.gpp) == (None, "cid-2", "DBAA")
    assert counting.keys == [f"T#{TICKET}"], "a guest has no consent row to read"
