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
def regional_client(service: str, region: str) -> Any:
    """:func:`client` in another region (a resource that lives in one region only, such as the demand gate's
    secrets in us-east-1)."""
    return boto3.client(service, region_name=region, config=_CONFIG)  # type: ignore[call-overload]


@cache
def table(name: str) -> Table:
    """A DynamoDB table resource (the resource API handles attribute (de)serialisation)."""
    return boto3.resource("dynamodb", config=_CONFIG).Table(name)


def execution_arn(state_machine_arn: str, name: str) -> str:
    """ARN of execution ``name`` of a standard Step Functions state machine."""
    return f"{state_machine_arn.replace(':stateMachine:', ':execution:', 1)}:{name}"
