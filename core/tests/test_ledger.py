"""The ledger's hash chain: any modified, dropped, inserted, duplicated or reordered record fails verification."""

import itertools
import json
import random
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from fast_channels.core.ledger import (
    GENESIS,
    ULID_ALPHABET,
    ChainedRecord,
    ChainHead,
    LedgerEntry,
    LedgerKind,
    Problem,
    canonical,
    digest,
    entry_from_item,
    entry_item,
    link,
    new_record_id,
    verify,
)

T0 = datetime(2026, 10, 29, 12, 0, 0, 123456, tzinfo=UTC)
RNG = random.Random(29)


def entries(chain: str, count: int, *, start: int = 0) -> list[LedgerEntry]:
    return [
        LedgerEntry(
            chain_id=chain,
            record_id=new_record_id(T0 + timedelta(seconds=i), RNG.randbytes(10)),
            kind=LedgerKind.RUNBOOK,
            at=T0 + timedelta(seconds=i),
            principal="arn:aws:iam::123456789012:role/runbook",
            action="runbook.dry_run",
            subject="fastm-demo",
            correlation_id=f"exec-{i}",
            detail={"step": i, "amount": "1.50", "nested": {"list": [1, "two", None, True]}},
        )
        for i in range(start, start + count)
    ]


def chain(items: list[LedgerEntry]) -> list[ChainedRecord]:
    records: list[ChainedRecord] = []
    head: ChainHead | None = None
    for entry in items:
        record = link(entry, head)
        head = record.head
        records.append(record)
    return records


def heads(records: list[ChainedRecord]) -> dict[str, ChainHead]:
    last: dict[str, ChainHead] = {}
    for record in records:
        if record.chain_id not in last or record.seq > last[record.chain_id].seq:
            last[record.chain_id] = record.head
    return last


@pytest.fixture(scope="module")
def ledger() -> list[ChainedRecord]:
    return chain(entries("release/fastm-demo", 7)) + chain(entries("money/line-1", 5))


def problems(records: list[ChainedRecord], last: dict[str, ChainHead]) -> set[Problem]:
    return {finding.problem for finding in verify(records, last).findings}


def test_a_linked_ledger_verifies(ledger: list[ChainedRecord]) -> None:
    result = verify(ledger, heads(ledger))

    assert result.ok
    assert result.chains == {"money/line-1": 5, "release/fastm-demo": 7}
    assert ledger[0].seq == 1
    assert ledger[0].prev_hash == GENESIS
    assert [r.prev_hash for r in ledger[1:7]] == [r.hash for r in ledger[:6]]
    assert all(digest(r) == r.hash for r in ledger)


def test_order_of_the_input_does_not_matter(ledger: list[ChainedRecord]) -> None:
    shuffled = list(ledger)
    RNG.shuffle(shuffled)

    assert verify(shuffled, heads(ledger)).ok


def test_byte_identical_copies_count_once(ledger: list[ChainedRecord]) -> None:
    result = verify([*ledger, ledger[3], ledger[3]], heads(ledger))

    assert result.ok
    assert result.chains["release/fastm-demo"] == 7


@pytest.mark.parametrize(
    "change",
    [
        {"subject": "fastm-other"},
        {"principal": "someone-else"},
        {"action": "runbook.apply"},
        {"detail": {"step": 99}},
        {"correlation_id": None},
        {"at": T0 + timedelta(days=1)},
        {"kind": LedgerKind.MONEY},
        {"seq": 40},
        {"prev_hash": "f" * 64},
    ],
)
def test_every_modified_record_fails(ledger: list[ChainedRecord], change: dict[str, object]) -> None:
    last = heads(ledger)
    for index, record in enumerate(ledger):
        tampered = [*ledger[:index], record.model_copy(update=change), *ledger[index + 1 :]]

        assert Problem.MODIFIED in problems(tampered, last), (index, change)


def test_a_modified_record_with_a_recomputed_hash_breaks_the_next_link(ledger: list[ChainedRecord]) -> None:
    rewritten = ledger[2].model_copy(update={"subject": "fastm-other"})
    rewritten = rewritten.model_copy(update={"hash": digest(rewritten)})

    assert problems([*ledger[:2], rewritten, *ledger[3:]], heads(ledger)) == {Problem.BROKEN_LINK}


def test_every_dropped_record_fails(ledger: list[ChainedRecord]) -> None:
    last = heads(ledger)
    for index in range(len(ledger)):
        found = problems(ledger[:index] + ledger[index + 1 :], last)

        assert found & {Problem.GAP, Problem.BAD_START, Problem.TRUNCATED}, index


def test_the_end_of_a_chain_is_only_known_from_its_head(ledger: list[ChainedRecord]) -> None:
    without_last = [r for r in ledger if r is not ledger[6]]

    assert verify(without_last).ok  # nothing in the records shows the tail is gone
    assert problems(without_last, heads(ledger)) == {Problem.TRUNCATED}


def test_a_chain_whose_records_are_all_missing_fails(ledger: list[ChainedRecord]) -> None:
    result = verify([r for r in ledger if r.chain_id == "money/line-1"], heads(ledger))

    assert [(f.chain_id, f.problem) for f in result.findings] == [("release/fastm-demo", Problem.NO_RECORDS)]


def test_every_reordering_fails(ledger: list[ChainedRecord]) -> None:
    last = heads(ledger)
    for i, j in itertools.combinations(range(len(ledger)), 2):
        a, b = ledger[i], ledger[j]
        if a.chain_id != b.chain_id:
            continue
        swapped = list(ledger)
        swapped[i] = a.model_copy(update={"seq": b.seq})
        swapped[j] = b.model_copy(update={"seq": a.seq})

        assert not verify(swapped, last).ok, (i, j)


def test_an_inserted_record_fails(ledger: list[ChainedRecord]) -> None:
    [extra] = entries("release/fastm-demo", 1, start=50)
    forged = link(extra, ledger[2].head)  # a valid link after record 3, colliding with record 4

    assert Problem.DUPLICATE in problems([*ledger, forged], heads(ledger))


def test_a_changed_copy_beside_its_original_fails(ledger: list[ChainedRecord]) -> None:
    copy = ledger[1].model_copy(update={"subject": "fastm-other"})

    assert problems([*ledger, copy], heads(ledger)) == {Problem.DUPLICATE, Problem.MODIFIED}


def test_link_refuses_another_chains_head(ledger: list[ChainedRecord]) -> None:
    [entry] = entries("release/fastm-demo", 1)

    with pytest.raises(ValueError, match="not money/line-1"):
        link(entry, ledger[-1].head)


def test_canonical_encoding_is_stable() -> None:
    [entry] = entries("runbook/r13", 1)
    record = link(entry, None)
    data = json.loads(canonical(record))

    assert "hash" not in data
    assert list(data) == sorted(data)
    assert data["at"] == "2026-10-29T12:00:00.123Z"
    assert data["seq"] == 1
    assert b" " not in canonical(record).replace(b"fastm demo", b"")
    assert ChainedRecord.model_validate_json(record.line()) == record
    assert record.line().endswith("}\n")


def test_times_are_utc_milliseconds() -> None:
    [entry] = entries("runbook/r13", 1)
    shifted = entry.model_copy(update={"at": T0.astimezone(timezone(timedelta(hours=2)))})

    assert LedgerEntry.model_validate(shifted.model_dump()).at == T0.replace(microsecond=123000)
    with pytest.raises(ValidationError, match="timezone-aware"):
        LedgerEntry.model_validate({**entry.model_dump(), "at": datetime(2026, 10, 29)})


@pytest.mark.parametrize("detail", [{"price": 1.5}, {"nested": [{"x": 0.1}]}, {"amount": Decimal("1.50")}])
def test_details_hold_no_floats_or_other_types(detail: dict[str, object]) -> None:
    [entry] = entries("runbook/r13", 1)

    with pytest.raises(ValidationError):
        LedgerEntry.model_validate({**entry.model_dump(), "detail": detail})


def test_record_ids_are_ulids_sorted_by_time() -> None:
    ids = [new_record_id(T0 + timedelta(milliseconds=ms), bytes(10)) for ms in (0, 1, 1000, 86_400_000)]

    assert ids == sorted(ids)
    assert all(len(i) == 26 and set(i) <= set(ULID_ALPHABET) for i in ids)
    assert new_record_id(datetime(1970, 1, 1, tzinfo=UTC), bytes(10)) == "0" * 26
    assert new_record_id(T0, b"\xff" * 10).endswith("Z" * 16)
    with pytest.raises(ValueError, match="10 bytes"):
        new_record_id(T0, bytes(9))
    with pytest.raises(ValueError, match="after the epoch"):
        new_record_id(datetime(1969, 1, 1, tzinfo=UTC), bytes(10))


def test_entry_items_round_trip_as_strings_only() -> None:
    [entry] = entries("money/line-1", 1)
    item = entry_item(entry)

    assert item["pk"] == "money/line-1"
    assert item["sk"] == f"E#{entry.record_id}"
    assert item["type"] == "entry"
    assert all(isinstance(value, str) for value in item.values())
    assert entry_from_item(item) == entry
    bare = entry.model_copy(update={"correlation_id": None, "detail": {}})
    assert "correlation_id" not in entry_item(bare)
    assert entry_from_item(entry_item(bare)) == bare


@pytest.mark.parametrize("change", [{"type": "head"}, {"sk": "HEAD"}])
def test_only_entry_items_are_entries(change: dict[str, str]) -> None:
    [entry] = entries("money/line-1", 1)

    with pytest.raises(ValueError, match="not a ledger entry item"):
        entry_from_item({**entry_item(entry), **change})
