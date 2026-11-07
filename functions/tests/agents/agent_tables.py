"""DynamoDB Local (``pds/dynamodb_local``) with the agents' and ledger tables, for the agents function tests."""

import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest

sys.path.append(str(Path(__file__).parents[1] / "pds"))

from dynamodb_local import _keys, dynamodb_endpoint, local_client

__all__ = ["AgentTables", "agent_tables", "dynamodb_endpoint"]


@dataclass(frozen=True)
class AgentTables:
    client: Any
    agents: str
    ledger: str
    endpoint: str

    def table(self, name: str) -> Any:
        """A table resource on DynamoDB Local (what ``functions.aws.table`` gives the ledger's ``append``)."""
        resource = boto3.resource(
            "dynamodb",
            endpoint_url=self.endpoint,
            region_name="us-east-1",
            aws_access_key_id="local",
            aws_secret_access_key="local",
        )
        return resource.Table(name)

    def ledger_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for page in self.client.get_paginator("scan").paginate(TableName=self.ledger, ConsistentRead=True):
            entries += [{k: next(iter(v.values())) for k, v in item.items()} for item in page["Items"]]
        return entries

    def counts(self) -> dict[str, int]:
        """Each cap item's count, by ``<pk>@<sk>``."""
        items = self.client.scan(TableName=self.agents, ConsistentRead=True)["Items"]
        return {f"{i['pk']['S']}@{i['sk']['S']}": int(i["n"]["N"]) for i in items}


@pytest.fixture
def agent_tables(dynamodb_endpoint: str) -> AgentTables:
    """Fresh agents (caps) and ledger tables."""
    client = local_client(dynamodb_endpoint)
    suffix = uuid.uuid4().hex[:8]
    agents, ledger = f"agents-{suffix}", f"ledger-{suffix}"
    client.create_table(TableName=agents, **_keys())
    client.create_table(TableName=ledger, **_keys())
    return AgentTables(client=client, agents=agents, ledger=ledger, endpoint=dynamodb_endpoint)
