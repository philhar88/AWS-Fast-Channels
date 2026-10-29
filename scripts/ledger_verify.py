#!/usr/bin/env -S uv run python
"""Verify a deployment's hash-chained ledger, write synthetic records to it, or purge a destroyed stage's archive.

    uv run python scripts/ledger_verify.py verify --prefix fastm                 # every chain in the archive
    uv run python scripts/ledger_verify.py verify --prefix fastm --chain test/x  # one chain
    uv run python scripts/ledger_verify.py write 1000 --prefix fastm             # append, wait, verify those chains
    uv run python scripts/ledger_verify.py purge --prefix fastci                 # what a destroy left (dry run)
    uv run python scripts/ledger_verify.py purge --prefix fastci --apply [--bypass-governance]

``verify`` reads every archived record (``ledger/`` in the archive bucket, gzip JSON lines that the ledger writer put
through Firehose) and every chain head in the ``<prefix>-ledger`` table, and checks with
``fast_channels.core.ledger.verify`` that each chain recomputes, runs from 1 without gaps or duplicates, links each
record to its predecessor and ends at its head. It exits 1 on any finding.

``write N`` appends N synthetic entries (kind ``test``) over ``--chains`` chains ``test/<run id>-<k>``, waits until
the writer has linked them and Firehose has archived them, and verifies those chains: the ledger's end-to-end check.

With Object Lock (``modules.ops.ledger_lock``) the archive outlives its stack, so ``make destroy`` leaves it.
``purge`` (for a destroyed stage only) deletes the archive's object versions whose retention has passed, then the
bucket once it is empty. ``--bypass-governance`` also deletes governance-mode versions before their retention ends,
which needs ``s3:BypassGovernanceRetention``; compliance-mode versions cannot be deleted before then by anyone.
"""

import argparse
import gzip
import os
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from _stack import Plane, Stage, add_prefix_argument, run, stage_id
from boto3.dynamodb.conditions import Attr

from fast_channels.core.ledger import (
    HEAD_KEY,
    ChainedRecord,
    ChainHead,
    LedgerEntry,
    LedgerKind,
    Verification,
    entry_item,
    new_record_id,
    verify,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
    from mypy_boto3_s3 import S3Client

ARCHIVE_PREFIX: Final = "ledger/"
"""``fast_channels.infra.constructs.ops.ledger.ARCHIVE_PREFIX`` (the scripts do not import the CDK app)."""
ARCHIVE_WAIT_SECONDS: Final = 600
"""How long ``write`` waits for the writer and Firehose (which buffers up to a minute)."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="verify the archived chains against their heads")
    check.add_argument("--chain", action="append", help="only this chain (repeatable)")
    write = commands.add_parser("write", help="append synthetic records, wait for the archive and verify them")
    write.add_argument("count", type=int, help="number of records")
    write.add_argument("--chains", type=int, default=4, help="spread over this many chains (default 4)")
    purge = commands.add_parser("purge", help="delete a destroyed stage's retained archive")
    purge.add_argument("--apply", action="store_true", help="delete (default: only list)")
    purge.add_argument("--bypass-governance", action="store_true", help="also delete governance-locked versions")
    for command in (check, write, purge):
        add_prefix_argument(command)
    args = parser.parse_args()
    stage = Stage.of(args.prefix)
    match args.command:
        case "verify":
            return report(check_archive(stage, set(args.chain) if args.chain else None))
        case "write":
            return write_and_verify(stage, args.count, args.chains)
        case _:
            return purge_archives(stage, apply=args.apply, bypass_governance=args.bypass_governance)


def ledger_table(stage: Stage) -> Table:
    resource = stage.session.resource("dynamodb", region_name=stage.region)
    return resource.Table(stage.output(Plane.DATA, "LedgerTableName"))


def s3_client(stage: Stage) -> S3Client:
    return stage.session.client("s3", region_name=stage.region)


def heads(table: Table) -> dict[str, ChainHead]:
    """Every chain's head item (a scan for ``sk = HEAD``)."""
    found: dict[str, ChainHead] = {}
    request: dict[str, Any] = {"FilterExpression": Attr("sk").eq(HEAD_KEY), "ConsistentRead": True}
    while True:
        page = table.scan(**request)
        for item in page.get("Items", []):
            chain = str(item["pk"])
            found[chain] = ChainHead(
                chain_id=chain, seq=int(str(item["seq"])), hash=str(item["hash"]), record_id=str(item["record_id"])
            )
        if "LastEvaluatedKey" not in page:
            return found
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def archived(s3: S3Client, bucket: str) -> Iterator[ChainedRecord]:
    """Every record in the archive (every object under ``ledger/``, gzip or plain JSON lines)."""
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=ARCHIVE_PREFIX):
        for item in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()
            text = (gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body).decode()
            yield from (ChainedRecord.model_validate_json(line) for line in text.splitlines() if line.strip())


def check_archive(stage: Stage, chains: set[str] | None = None) -> Verification:
    """Verify the archive's chains (``chains`` only, if given) against the table's heads."""
    records = archived(s3_client(stage), stage.output(Plane.DATA, "LedgerArchiveBucketName"))
    last = heads(ledger_table(stage))
    if chains is not None:
        return verify((r for r in records if r.chain_id in chains), {c: h for c, h in last.items() if c in chains})
    return verify(records, last)


def report(result: Verification) -> int:
    for chain, count in result.chains.items():
        print(f"  {chain:<48} {count:>7} records")
    for finding in result.findings:
        print(
            f"  {finding.problem.value:<12} {finding.chain_id} seq {finding.seq} ({finding.record_id})", file=sys.stderr
        )
    print(
        f"{len(result.chains)} chains, {sum(result.chains.values())} records: {'verified' if result.ok else 'FAILED'}"
    )
    return 0 if result.ok else 1


def synthetic_entries(count: int, chains: int, run_id: str) -> list[LedgerEntry]:
    """``count`` test entries spread round-robin over ``chains`` chains ``test/<run id>-<k>``."""
    entries = []
    for i in range(count):
        now = datetime.now(UTC)
        entries.append(
            LedgerEntry(
                chain_id=f"test/{run_id}-{i % chains}",
                record_id=new_record_id(now, os.urandom(10)),
                kind=LedgerKind.TEST,
                at=now,
                principal="scripts/ledger_verify.py",
                action="ledger.test",
                subject=f"synthetic-{i}",
                correlation_id=run_id,
                detail={"index": i, "amount": f"{i % 100}.{i % 7:02d}"},
            )
        )
    return entries


def write_and_verify(stage: Stage, count: int, chains: int) -> int:
    run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{os.urandom(3).hex()}"  # lower case, as chain ids are
    table = ledger_table(stage)
    entries = synthetic_entries(count, chains, run_id)
    started = time.monotonic()
    with table.batch_writer() as batch:  # new keys only: a ULID per entry
        for entry in entries:
            batch.put_item(Item=entry_item(entry))
    print(f"appended {count} records over {chains} chains test/{run_id}-* in {time.monotonic() - started:.1f} s")
    names = {entry.chain_id for entry in entries}
    expected = {name: sum(1 for e in entries if e.chain_id == name) for name in names}
    deadline = time.monotonic() + ARCHIVE_WAIT_SECONDS
    while True:
        result = check_archive(stage, names)
        if result.chains == expected or time.monotonic() > deadline:
            break
        time.sleep(15)
    print(f"archived after {time.monotonic() - started:.0f} s")
    return report(result)


def retained_archives(stage: Stage, s3: S3Client) -> list[str]:
    """Buckets the stage's ledger archive left behind (``<stage id>-data-ledgerarchive…``)."""
    start = f"{stage_id(stage.prefix).lower()}-data-ledgerarchive"
    return [bucket["Name"] for bucket in s3.list_buckets()["Buckets"] if bucket["Name"].startswith(start)]


def purge_archives(stage: Stage, *, apply: bool, bypass_governance: bool) -> int:
    if stage.status(Plane.DATA) is not None:
        print(f"Refusing: {stage.stack_name(Plane.DATA)} exists; purge only what `make destroy` left.", file=sys.stderr)
        return 1
    s3 = s3_client(stage)
    status = 0
    for bucket in retained_archives(stage, s3):
        versions = list(object_versions(s3, bucket))
        now = datetime.now(UTC)
        deletable = [
            v for v in versions if bypass_governance or v.get("retain_until") is None or v["retain_until"] <= now
        ]
        print(f"{bucket}: {len(versions)} versions, {len(deletable)} deletable now")
        if not apply:
            continue
        for version in deletable:
            s3.delete_object(
                Bucket=bucket, Key=version["key"], VersionId=version["id"], BypassGovernanceRetention=bypass_governance
            )
        if len(deletable) == len(versions):
            s3.delete_bucket(Bucket=bucket)
            print(f"{bucket}: deleted")
        else:
            print(f"{bucket}: {len(versions) - len(deletable)} versions still locked", file=sys.stderr)
            status = 1
    return status


def object_versions(s3: S3Client, bucket: str) -> Iterator[dict[str, Any]]:
    """Every object version and delete marker, with its Object Lock retain-until date (``None`` if unlocked)."""
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        for marker in page.get("DeleteMarkers", []):
            yield {"key": marker["Key"], "id": marker["VersionId"], "retain_until": None}
        for version in page.get("Versions", []):
            head = s3.head_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
            yield {
                "key": version["Key"],
                "id": version["VersionId"],
                "retain_until": head.get("ObjectLockRetainUntilDate"),
            }


if __name__ == "__main__":
    run(main)
