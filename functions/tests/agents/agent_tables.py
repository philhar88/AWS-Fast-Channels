"""DynamoDB Local (``pds/dynamodb_local``) with the agents', ledger and library tables, for the agents function
tests."""

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
    library: str = ""

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

    def store_plan(self, plan_id: str, *, start_ms: int, expires_at: int) -> None:
        """A stored plan, as ``schedule___plan`` writes it (``CHANNEL#<channel>`` / ``PLAN#<plan_id>``)."""
        channel = plan_id.rpartition(".")[0]
        self.table(self.library).put_item(
            Item={
                "pk": f"CHANNEL#{channel}",
                "sk": f"PLAN#{plan_id}",
                "plan": {"channel": channel, "start_ms": start_ms},
                "expires_at": expires_at,
            }
        )

    def counts(self) -> dict[str, int]:
        """Each cap item's count, by ``<pk>@<sk>``."""
        items = self.client.scan(TableName=self.agents, ConsistentRead=True)["Items"]
        return {f"{i['pk']['S']}@{i['sk']['S']}": int(i["n"]["N"]) for i in items}


@pytest.fixture
def agent_tables(dynamodb_endpoint: str) -> AgentTables:
    """Fresh agents (caps), ledger and library (plans) tables."""
    client = local_client(dynamodb_endpoint)
    suffix = uuid.uuid4().hex[:8]
    agents, ledger, library = f"agents-{suffix}", f"ledger-{suffix}", f"library-{suffix}"
    for name in (agents, ledger, library):
        client.create_table(TableName=name, **_keys())
    return AgentTables(client=client, agents=agents, ledger=ledger, endpoint=dynamodb_endpoint, library=library)
