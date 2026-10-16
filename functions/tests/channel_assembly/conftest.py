"""Fixtures for the Channel Assembly custom resources and the as-run reconciler: botocore Stubbers on the shared
clients, a Lambda context and CloudFormation custom-resource events."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import Stubber

from fast_channels.functions import aws
from fast_channels.functions.settings import settings

TABLE = "library"
STACK_ID = "arn:aws:cloudformation:us-east-1:123456789012:stack/FastChannels-Media/1a2b"
_SERIALIZER = TypeSerializer()


@dataclass
class Context:
    """The parts of a Lambda context that Powertools and the handlers read."""

    remaining_ms: int = 300_000
    function_name: str = "test"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:test"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/test"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return self.remaining_ms


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("LIBRARY_TABLE", TABLE)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def context() -> Context:
    return Context()


@pytest.fixture
def stub() -> Iterator[Callable[[str], Stubber]]:
    """``stub("mediatailor")`` (or ``stub("dynamodb")`` for the library table): an active Stubber on the client
    the handlers use. Every queued response must be consumed by the test."""
    stubbers: dict[str, Stubber] = {}

    def get(service: str) -> Stubber:
        if service not in stubbers:
            client = aws.table(TABLE).meta.client if service == "dynamodb" else aws.client(service)
            stubbers[service] = Stubber(client)
            stubbers[service].activate()
        return stubbers[service]

    try:
        yield get
    finally:  # the clients are shared: always detach the stubbers before asserting
        for stubber in stubbers.values():
            stubber.deactivate()
    for stubber in stubbers.values():
        stubber.assert_no_pending_responses()


def _typed(item: dict[str, Any]) -> dict[str, Any]:
    return {key: _SERIALIZER.serialize(value) for key, value in item.items()}


def _cfn_event(
    resource_type: str,
    request_type: str,
    properties: dict[str, Any],
    *,
    old: dict[str, Any] | None = None,
    physical_id: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "RequestType": request_type,
        "ServiceToken": "arn:aws:lambda:us-east-1:123456789012:function:framework",
        "ResponseURL": "https://cloudformation-custom-resource-response.example/",
        "StackId": STACK_ID,
        "RequestId": f"{request_type.lower()}-request",
        "LogicalResourceId": "Resource",
        "ResourceType": resource_type,
        "ResourceProperties": {"ServiceToken": "token", **properties},
    }
    if old is not None:
        event["OldResourceProperties"] = {"ServiceToken": "token", **old}
    if physical_id is not None:
        event["PhysicalResourceId"] = physical_id
    return event


def _client_error(code: str, message: str = "", status: int = 400) -> dict[str, Any]:
    return {"service_error_code": code, "service_message": message or code, "http_status_code": status}


@pytest.fixture
def typed() -> Callable[[dict[str, Any]], dict[str, Any]]:
    """DynamoDB wire format of a plain item (what the table resource sends)."""
    return _typed


@pytest.fixture
def cfn_event() -> Callable[..., dict[str, Any]]:
    """A custom-resource request as the CDK Provider framework passes it (property values are strings):
    ``cfn_event(resource_type, request_type, properties, old=None, physical_id=None)``."""
    return _cfn_event


@pytest.fixture
def client_error() -> Callable[..., dict[str, Any]]:
    """``Stubber.add_client_error`` keyword arguments: ``client_error(code, message="", status=400)``."""
    return _client_error
