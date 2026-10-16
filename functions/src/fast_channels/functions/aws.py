"""Shared, lazily created AWS SDK clients (one per service per execution environment)."""

from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
else:
    Table = Any

_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 8}, connect_timeout=5, read_timeout=30)


@cache
def client(service: str) -> Any:
    """A boto3 client with adaptive retries (MediaTailor and MediaConvert have low per-API TPS quotas)."""
    return boto3.client(service, config=_CONFIG)  # type: ignore[call-overload]


@cache
def table(name: str) -> Table:
    """A DynamoDB table resource (the resource API handles attribute (de)serialisation)."""
    return boto3.resource("dynamodb", config=_CONFIG).Table(name)
