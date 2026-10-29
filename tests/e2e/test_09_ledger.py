"""The ops module's ledger, on stages that enable it (skipped otherwise): entries appended with the producers' client
are chained by the writer, archived by Firehose under Object Lock, and verify against their chains' heads.

Independent of the channel tests. Firehose buffers up to a minute, so the archive check waits up to five minutes.
"""

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import boto3
import pytest
from _stack import Module, Plane, Stage
from harness import wait_for
from ledger_verify import check_archive, synthetic_entries

from fast_channels.functions.ops import ledger

if TYPE_CHECKING:
    from fast_channels.core.ledger import Verification

pytestmark = pytest.mark.e2e

RECORDS: Final = 20
CHAINS: Final = 2
ARCHIVE_TIMEOUT_S: Final = 300


@pytest.fixture(scope="module")
def ops(stage: Stage) -> Stage:
    if stage.status(Module.OPS) is None:
        pytest.skip(f"stage {stage.prefix} has no Ops stack (modules.ops.enabled is false)")
    # The producers' client uses the functions' shared clients, on boto3's default session (which ignores
    # AWS_REGION): put it in the stage's region, as scripts/cleanup.py does.
    boto3.setup_default_session(region_name=stage.region)
    return stage


def test_appended_entries_are_chained_archived_locked_and_verified(ops: Stage, run_id: str) -> None:
    table = ops.output(Plane.DATA, "LedgerTableName")
    chain_run = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{os.urandom(2).hex()}"
    entries = synthetic_entries(RECORDS, CHAINS, chain_run)
    for entry in entries:
        ledger.append(entry.model_copy(update={"correlation_id": run_id}), table=table)
    with pytest.raises(ledger.DuplicateEntryError):
        ledger.append(entries[0], table=table)  # never overwritten
    chains = {entry.chain_id for entry in entries}

    def archived() -> Verification | None:
        result = check_archive(ops, chains)
        return result if sum(result.chains.values()) == RECORDS else None

    result = wait_for(archived, timeout=ARCHIVE_TIMEOUT_S, interval=20, what=f"{RECORDS} archived ledger records")

    assert result.ok, result.findings
    assert result.chains == dict.fromkeys(chains, RECORDS // CHAINS)
    s3 = ops.session.client("s3", region_name=ops.region)
    bucket = ops.output(Plane.DATA, "LedgerArchiveBucketName")
    newest = max(s3.list_objects_v2(Bucket=bucket, Prefix="ledger/")["Contents"], key=lambda o: o["LastModified"])
    head = s3.head_object(Bucket=bucket, Key=newest["Key"])
    assert head["ObjectLockMode"] in {"GOVERNANCE", "COMPLIANCE"}
    assert head["ObjectLockRetainUntilDate"] > datetime.now(UTC)
