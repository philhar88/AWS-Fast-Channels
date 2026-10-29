"""DynamoDB Local for the reservation tests: one container per session, fresh tables per test.

The tests that take the ``dynamodb`` fixture run against ``amazon/dynamodb-local`` in Docker (in memory), so the
conditions and transactions are DynamoDB's own, not a mock's. Without Docker they are skipped.

The session removes its container when it ends. A session that is killed cannot, so every container carries
``dynamodb_local.LABEL``, and a session first removes the labelled ones older than ``dynamodb_local.STALE``.
"""

import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from fast_channels.core.pds.names import PENDING_INDEX

sys.path.append(str(Path(__file__).parent))

from dynamodb_local import LABEL, stale

IMAGE: Final = "amazon/dynamodb-local:3.3.2"
STARTUP_SECONDS: Final = 60


def _docker(*args: str, timeout: float = 300) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


@pytest.fixture(scope="session")
def dynamodb_endpoint() -> Iterator[str]:
    """The endpoint of a DynamoDB Local container (skips the test without Docker)."""
    if shutil.which("docker") is None:
        pytest.skip("DynamoDB Local needs Docker")
    try:
        listing = _docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.ID}} {{.CreatedAt}}")
        for container in stale(listing, datetime.now(UTC)):
            _docker("rm", "-f", container, timeout=60)
        container = _docker(
            "run",
            "-d",
            "--rm",
            "--label",
            LABEL,
            "-p",
            "127.0.0.1::8000",
            IMAGE,
            "-jar",
            "DynamoDBLocal.jar",
            "-inMemory",
        )
    except (subprocess.SubprocessError, OSError) as error:
        pytest.skip(f"DynamoDB Local could not start: {error}")
    try:
        port = _docker("port", container, "8000/tcp").splitlines()[0].rsplit(":", 1)[1]
        endpoint = f"http://127.0.0.1:{port}"
        probe = local_client(endpoint)
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            try:
                probe.list_tables()
                break
            except BotoCoreError, ClientError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield endpoint
    finally:
        _docker("rm", "-f", container, timeout=60)


def local_client(endpoint: str) -> Any:
    return boto3.client(
        "dynamodb",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
        config=Config(retries={"mode": "standard", "total_max_attempts": 3}, max_pool_connections=32),
    )


@dataclass(frozen=True)
class Tables:
    """A PDS table and a ledger table of one test, in DynamoDB Local."""

    client: Any
    pds: str
    ledger: str

    def item(self, key: dict[str, str]) -> dict[str, Any] | None:
        found: dict[str, Any] | None = self.client.get_item(
            TableName=self.pds, Key={k: {"S": v} for k, v in key.items()}, ConsistentRead=True
        ).get("Item")
        return found

    def ledger_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for page in self.client.get_paginator("scan").paginate(TableName=self.ledger, ConsistentRead=True):
            entries.extend(page["Items"])
        return entries


def _keys() -> dict[str, Any]:
    return {
        "AttributeDefinitions": [
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        "BillingMode": "PAY_PER_REQUEST",
    }


@pytest.fixture
def dynamodb(dynamodb_endpoint: str) -> Tables:
    """Fresh ``<prefix>-pds`` (with its ``pending`` index) and ledger tables."""
    client = local_client(dynamodb_endpoint)
    suffix = uuid.uuid4().hex[:8]
    pds, ledger = f"pds-{suffix}", f"ledger-{suffix}"
    keys = _keys()
    client.create_table(
        TableName=pds,
        **{
            **keys,
            "AttributeDefinitions": [
                *keys["AttributeDefinitions"],
                {"AttributeName": "pending", "AttributeType": "S"},
                {"AttributeName": "pending_at", "AttributeType": "S"},
            ],
        },
        GlobalSecondaryIndexes=[
            {
                "IndexName": PENDING_INDEX,
                "KeySchema": [
                    {"AttributeName": "pending", "KeyType": "HASH"},
                    {"AttributeName": "pending_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )
    client.create_table(TableName=ledger, **keys)
    return Tables(client=client, pds=pds, ledger=ledger)
