"""The ledger writer and the producers' client, against an in-memory ledger table that applies the conditional
writes they use: whatever the redeliveries, failures and interleavings of writers, the archive verifies."""

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from fast_channels.core.ledger import ChainedRecord, ChainHead, LedgerEntry, LedgerKind, entry_item, verify
from fast_channels.functions import aws
from fast_channels.functions.ops import ledger, ledger_writer
from fast_channels.functions.settings import settings

TABLE = "fastm-ledger"
STREAM = "fastm-ledger"
T0 = datetime(2026, 10, 29, 12, tzinfo=UTC)


def _conflict(code: str = "ConditionalCheckFailedException") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "condition"}}, "Op")


@dataclass
class FakeLedger:
    """``get_item``, ``put_item`` and ``meta.client.transact_write_items`` with the writer's conditions."""

    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    fail_next_transaction: bool = False

    @property
    def meta(self) -> Any:
        return self

    @property
    def client(self) -> Any:
        return self

    def get_item(self, *, Key: dict[str, str], ConsistentRead: bool = False) -> dict[str, Any]:  # noqa: N803
        assert ConsistentRead
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def _allowed(self, put: dict[str, Any]) -> bool:
        item = put["Item"]
        current = self.items.get((item["pk"], item["sk"]))
        match put["ConditionExpression"]:
            case "attribute_not_exists(pk)":
                return current is None
            case "seq = :seq AND #hash = :hash":
                values = put["ExpressionAttributeValues"]
                return current is not None and (current["seq"], current["hash"]) == (values[":seq"], values[":hash"])
        raise AssertionError(put["ConditionExpression"])

    def put_item(self, *, Item: dict[str, Any], ConditionExpression: str) -> None:  # noqa: N803
        if not self._allowed({"Item": Item, "ConditionExpression": ConditionExpression}):
            raise _conflict()
        self.items[Item["pk"], Item["sk"]] = dict(Item)

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]]) -> None:  # noqa: N803
        if self.fail_next_transaction:
            self.fail_next_transaction = False
            raise _conflict("ProvisionedThroughputExceededException")
        puts = [item["Put"] for item in TransactItems]
        assert all(put["TableName"] == TABLE for put in puts)
        if not all(self._allowed(put) for put in puts):
            raise _conflict("TransactionCanceledException")
        for put in puts:
            self.items[put["Item"]["pk"], put["Item"]["sk"]] = dict(put["Item"])

    def heads(self) -> dict[str, ChainHead]:
        return {
            pk: ChainHead(chain_id=pk, seq=item["seq"], hash=item["hash"], record_id=item["record_id"])
            for (pk, sk), item in self.items.items()
            if sk == "HEAD"
        }


@dataclass
class FakeFirehose:
    lines: list[str] = field(default_factory=list)
    fail: int = 0

    def put_record(self, *, DeliveryStreamName: str, Record: dict[str, bytes]) -> dict[str, str]:  # noqa: N803
        assert DeliveryStreamName == STREAM
        if self.fail:
            self.fail -= 1
            raise _conflict("ServiceUnavailableException")
        self.lines.append(Record["Data"].decode())
        return {"RecordId": str(len(self.lines))}

    def archive(self) -> list[ChainedRecord]:
        return [ChainedRecord.model_validate_json(line) for line in self.lines]


@dataclass
class Context:
    function_name: str = "ledger-writer"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:ledger-writer"
    aws_request_id: str = "request-1"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeLedger, FakeFirehose]]:
    table, firehose = FakeLedger(), FakeFirehose()
    monkeypatch.setenv("LEDGER_TABLE", TABLE)
    monkeypatch.setenv("LEDGER_STREAM", STREAM)
    monkeypatch.setattr(aws, "table", lambda name: table)
    monkeypatch.setattr(aws, "client", lambda service: firehose)
    settings.cache_clear()
    yield table, firehose
    settings.cache_clear()


def entry(chain: str, i: int) -> LedgerEntry:
    return ledger.entry(
        chain,
        LedgerKind.TEST,
        principal="test",
        action="ledger.test",
        subject=f"record-{i}",
        correlation_id=f"run-{i}",
        detail={"i": i},
        at=T0 + timedelta(milliseconds=i),
    )


_SERIALIZER = TypeSerializer()


def stream_record(item: dict[str, Any], *, sequence: int, event: str = "INSERT") -> dict[str, Any]:
    image = {key: _SERIALIZER.serialize(value) for key, value in item.items()}
    images = {"OldImage": image} if event == "REMOVE" else {"NewImage": image}
    if event == "MODIFY":
        images["OldImage"] = image
    return {
        "eventID": str(sequence),
        "eventName": event,
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "Keys": {"pk": image["pk"], "sk": image["sk"]},
            "SequenceNumber": str(sequence),
            "StreamViewType": "NEW_AND_OLD_IMAGES",
            **images,
        },
    }


def batch(*records: dict[str, Any]) -> dict[str, Any]:
    return {"Records": list(records)}


def test_append_puts_only_new_entries(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    table, _ = fake
    first = ledger.append(entry("runbook/r13", 1))

    assert table.items[("runbook/r13", f"E#{first.record_id}")] == entry_item(first)
    with pytest.raises(ledger.DuplicateEntryError, match="already in the ledger"):
        ledger.append(first)


def test_append_names_the_table_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEDGER_TABLE", raising=False)
    settings.cache_clear()

    with pytest.raises(RuntimeError, match="LEDGER_TABLE"):
        ledger.append(entry("runbook/r13", 1))
    settings.cache_clear()


def test_append_passes_other_errors_through(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def put_item(self, **_: Any) -> None:
            raise _conflict("AccessDeniedException")

    monkeypatch.setattr(aws, "table", lambda name: Broken())

    with pytest.raises(ClientError, match="AccessDenied"):
        ledger.append(entry("runbook/r13", 1), table=TABLE)


def test_entries_are_chained_and_archived_in_stream_order(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    table, firehose = fake
    items = [entry_item(entry("release/fastm-demo", i)) for i in range(5)]

    result = ledger_writer.handler(batch(*(stream_record(item, sequence=i) for i, item in enumerate(items))), Context())

    assert result == {"batchItemFailures": []}
    archive = firehose.archive()
    assert [r.seq for r in archive] == [1, 2, 3, 4, 5]
    assert [r.subject for r in archive] == [f"record-{i}" for i in range(5)]
    assert verify(archive, table.heads()).ok
    assert table.heads()["release/fastm-demo"].seq == 5
    assert all(line.endswith("\n") for line in firehose.lines)


def test_a_redelivered_entry_is_archived_again_unchanged(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    table, firehose = fake
    item = entry_item(entry("release/fastm-demo", 0))
    firehose.fail = 1  # linked, then the archive put fails: the record is retried

    first = ledger_writer.handler(batch(stream_record(item, sequence=1)), Context())
    second = ledger_writer.handler(batch(stream_record(item, sequence=1)), Context())

    assert first == {"batchItemFailures": [{"itemIdentifier": "1"}]}
    assert second == {"batchItemFailures": []}
    assert table.heads()["release/fastm-demo"].seq == 1
    assert len(firehose.lines) == 1
    third = ledger_writer.handler(batch(stream_record(item, sequence=1)), Context())  # at-least-once delivery
    assert third == {"batchItemFailures": []}
    assert firehose.lines[0] == firehose.lines[1]
    assert verify(firehose.archive(), table.heads()).ok


def test_the_first_failure_ends_the_batch(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    table, firehose = fake
    items = [entry_item(entry("money/line-1", i)) for i in range(3)]
    table.fail_next_transaction = True

    result = ledger_writer.handler(
        batch(*(stream_record(item, sequence=10 + i) for i, item in enumerate(items))), Context()
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "10"}]}
    assert firehose.lines == []
    assert ledger_writer.handler(
        batch(*(stream_record(i, sequence=10 + n) for n, i in enumerate(items))), Context()
    ) == {"batchItemFailures": []}
    assert [r.seq for r in firehose.archive()] == [1, 2, 3]


def test_a_writer_that_lost_the_race_retries_after_the_new_head(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    table, firehose = fake
    a, b = (entry_item(entry("release/fastm-demo", i)) for i in range(2))
    stale = ledger_writer.head("release/fastm-demo")  # None: both writers read an empty chain
    ledger_writer.handler(batch(stream_record(a, sequence=1)), Context())  # writer 1 wins

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ledger_writer, "head", lambda chain_id: stale)
        lost = ledger_writer.handler(batch(stream_record(b, sequence=2)), Context())  # writer 2 linked after None

    assert lost == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert ledger_writer.handler(batch(stream_record(b, sequence=2)), Context()) == {"batchItemFailures": []}
    assert [r.seq for r in firehose.archive()] == [1, 2]
    assert verify(firehose.archive(), table.heads()).ok


def test_changed_and_invalid_entries_are_reported_not_chained(fake: tuple[FakeLedger, FakeFirehose]) -> None:
    _, firehose = fake
    item = entry_item(entry("runbook/r12", 0))
    invalid = {**item, "sk": "E#not-a-ulid"}

    result = ledger_writer.handler(
        batch(
            stream_record(item, sequence=1, event="MODIFY"),
            stream_record(item, sequence=2, event="REMOVE"),
            stream_record(invalid, sequence=3),
            stream_record(item, sequence=4),
        ),
        Context(),
    )

    assert result == {"batchItemFailures": []}
    assert [r.record_id for r in firehose.archive()] == [item["sk"].removeprefix("E#")]


def test_random_interleavings_of_writers_redeliveries_and_failures_always_verify(
    fake: tuple[FakeLedger, FakeFirehose],
) -> None:
    table, firehose = fake
    rng = random.Random(1029)
    pending = [entry_item(entry(chain, i)) for i in range(40) for chain in ("release/a", "release/b", "money/c")]
    sequence = 0
    while pending:
        size = rng.randint(1, 6)
        group, pending = pending[:size], pending[size:]
        table.fail_next_transaction = rng.random() < 0.2
        firehose.fail = int(rng.random() < 0.2)
        records = [stream_record(item, sequence=(sequence := sequence + 1)) for item in group]
        result = ledger_writer.handler(batch(*records), Context())
        if failures := result["batchItemFailures"]:
            failed = next(
                i for i, r in enumerate(records) if r["dynamodb"]["SequenceNumber"] == failures[0]["itemIdentifier"]
            )
            pending = group[failed:] + pending  # Lambda retries from the failed record
        if rng.random() < 0.3:
            ledger_writer.handler(batch(*records[:1]), Context())  # a stray redelivery

    archive = firehose.archive()
    result = verify(archive, table.heads())
    assert result.ok, result.findings
    assert result.chains == {"money/c": 40, "release/a": 40, "release/b": 40}
