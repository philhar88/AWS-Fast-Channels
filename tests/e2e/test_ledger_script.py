"""Offline tests of ``scripts/ledger_verify.py`` (not marked e2e), with in-memory stand-ins for S3 and the table."""

import gzip
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from _stack import Stage

from fast_channels.core.ledger import ChainedRecord, ChainHead, link

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fast_script_ledger_verify", SCRIPTS / "ledger_verify.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ledger_verify = _script()


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakeS3:
    """``list_objects_v2`` and ``list_object_versions`` pages, ``get_object``, ``head_object``, deletes."""

    def __init__(self, objects: dict[str, bytes], *, retain_until: datetime | None = None) -> None:
        self.objects = objects
        self.retain_until = retain_until
        self.deleted: list[tuple[str, bool]] = []
        self.deleted_buckets: list[str] = []

    def get_paginator(self, operation: str) -> Any:
        s3 = self

        class Paginator:
            def paginate(self, **_: Any) -> list[dict[str, Any]]:
                if operation == "list_objects_v2":
                    return [{"Contents": [{"Key": key} for key in s3.objects]}]
                return [
                    {
                        "Versions": [{"Key": key, "VersionId": "v1"} for key in s3.objects],
                        "DeleteMarkers": [{"Key": "ledger/gone", "VersionId": "m1"}],
                    }
                ]

        return Paginator()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"Body": FakeBody(self.objects[Key])}

    def head_object(self, **_: Any) -> dict[str, Any]:
        return {"ObjectLockRetainUntilDate": self.retain_until} if self.retain_until else {}

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": [{"Name": "fastchannels-fastx-data-ledgerarchive1a2b-xyz"}, {"Name": "other"}]}

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str, BypassGovernanceRetention: bool) -> None:  # noqa: N803
        self.deleted.append((Key, BypassGovernanceRetention))

    def delete_bucket(self, *, Bucket: str) -> None:  # noqa: N803
        self.deleted_buckets.append(Bucket)


class FakeTable:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.requests: list[dict[str, Any]] = []

    def scan(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.pages[len(self.requests) - 1]


def chain_of(entries: list[Any]) -> list[ChainedRecord]:
    records, head = [], None
    for entry in entries:
        record = link(entry, head)
        head = record.head
        records.append(record)
    return records


def test_synthetic_entries_spread_over_chains() -> None:
    entries = ledger_verify.synthetic_entries(10, 3, "run")

    assert [e.chain_id for e in entries[:4]] == ["test/run-0", "test/run-1", "test/run-2", "test/run-0"]
    assert len({e.record_id for e in entries}) == 10
    assert all(e.correlation_id == "run" and e.kind == "test" for e in entries)


def test_archived_reads_gzip_and_plain_lines() -> None:
    records = chain_of(ledger_verify.synthetic_entries(3, 1, "run"))
    s3 = FakeS3(
        {
            "ledger/2026/10/29/a.gz": gzip.compress("".join(r.line() for r in records[:2]).encode()),
            "ledger/2026/10/29/b": records[2].line().encode() + b"\n",
        }
    )

    assert list(ledger_verify.archived(s3, "bucket")) == records


def test_heads_scan_every_page() -> None:
    table = FakeTable(
        [
            {
                "Items": [{"pk": "a", "sk": "HEAD", "seq": 2, "hash": "1" * 64, "record_id": "r2"}],
                "LastEvaluatedKey": {"pk": "a"},
            },
            {"Items": [{"pk": "b", "sk": "HEAD", "seq": 1, "hash": "2" * 64, "record_id": "r1"}]},
        ]
    )

    found = ledger_verify.heads(table)

    assert found == {
        "a": ChainHead(chain_id="a", seq=2, hash="1" * 64, record_id="r2"),
        "b": ChainHead(chain_id="b", seq=1, hash="2" * 64, record_id="r1"),
    }
    assert table.requests[1]["ExclusiveStartKey"] == {"pk": "a"}


def test_report_exits_1_on_findings(capsys: pytest.CaptureFixture[str]) -> None:
    records = chain_of(ledger_verify.synthetic_entries(3, 1, "run"))

    assert ledger_verify.report(ledger_verify.verify(records)) == 0
    assert ledger_verify.report(ledger_verify.verify(records[:1] + records[2:])) == 1
    out = capsys.readouterr()
    assert "1 chains, 3 records: verified" in out.out
    assert "gap" in out.err


class FakeStage:
    prefix = "fastx"

    def __init__(self, deployed: bool) -> None:
        self.deployed = deployed

    def status(self, plane: Any) -> str | None:
        return "CREATE_COMPLETE" if self.deployed else None

    def stack_name(self, plane: Any) -> str:
        return f"FastChannels-fastx-{plane}"


def test_purge_refuses_while_the_data_stack_exists(capsys: pytest.CaptureFixture[str]) -> None:
    assert ledger_verify.purge_archives(FakeStage(deployed=True), apply=True, bypass_governance=False) == 1
    assert "Refusing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("retain_until", "bypass", "deleted", "status"),
    [
        (datetime.now(UTC) - timedelta(days=1), False, 3, 0),  # retention over: everything goes, then the bucket
        (datetime.now(UTC) + timedelta(days=1), False, 1, 1),  # still locked: only the delete marker
        (datetime.now(UTC) + timedelta(days=1), True, 3, 0),  # governance bypass
    ],
)
def test_purge_deletes_what_retention_allows(
    monkeypatch: pytest.MonkeyPatch, retain_until: datetime, bypass: bool, deleted: int, status: int
) -> None:
    s3 = FakeS3({"ledger/a.gz": b"", "ledger/b.gz": b""}, retain_until=retain_until)
    monkeypatch.setattr(ledger_verify, "s3_client", lambda stage: s3)

    assert ledger_verify.purge_archives(FakeStage(deployed=False), apply=True, bypass_governance=bypass) == status
    assert len(s3.deleted) == deleted
    assert all(flag is bypass for _, flag in s3.deleted)
    assert s3.deleted_buckets == (["fastchannels-fastx-data-ledgerarchive1a2b-xyz"] if status == 0 else [])


def test_purge_lists_without_apply(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    s3 = FakeS3({"ledger/a.gz": b""}, retain_until=datetime.now(UTC) + timedelta(days=1))
    monkeypatch.setattr(ledger_verify, "s3_client", lambda stage: s3)

    assert ledger_verify.purge_archives(FakeStage(deployed=False), apply=False, bypass_governance=False) == 0
    assert s3.deleted == []
    assert "2 versions, 1 deletable now" in capsys.readouterr().out


def test_stage_is_the_stack_helper() -> None:
    assert ledger_verify.Stage is Stage
