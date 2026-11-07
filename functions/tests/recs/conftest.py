"""The recommender tests' fixtures: a stand-in Bedrock Runtime client (``fake_bedrock``), and tables in DynamoDB
Local (the PDS tests' container, ``functions/tests/pds/dynamodb_local.py``; skipped without Docker)."""

import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest

sys.path.append(str(Path(__file__).parent))
sys.path.append(str(Path(__file__).parents[1] / "pds"))

from dynamodb_local import dynamodb_endpoint, local_client
from fake_bedrock import FakeBedrock

__all__ = ["bedrock", "dynamodb_endpoint", "tables"]


@pytest.fixture
def bedrock() -> FakeBedrock:
    return FakeBedrock()


@dataclass(frozen=True)
class Tables:
    """The recommender's table, an identity table (consent rows) and a counter table of one test."""

    client: Any
    recs: Any
    identity: Any
    counters: Any


@pytest.fixture
def tables(dynamodb_endpoint: str) -> Iterator[Tables]:
    client = local_client(dynamodb_endpoint)
    resource = boto3.resource(
        "dynamodb",
        endpoint_url=dynamodb_endpoint,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    suffix = uuid.uuid4().hex[:8]
    names = {"recs": f"recs-{suffix}", "identity": f"identity-{suffix}", "counters": f"models-{suffix}"}
    for name in names.values():
        client.create_table(
            TableName=name,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
    yield Tables(client=client, **{role: resource.Table(name) for role, name in names.items()})
    for name in names.values():
        client.delete_table(TableName=name)
