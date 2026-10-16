"""Fixtures for the ingest steps: environment, botocore-stubbed AWS clients and an in-memory object store."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.stub import Stubber

from fast_channels.core.ladder import Ladder
from fast_channels.core.naming import ladder_path
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

DATA = Path(__file__).parent / "data"
REGION = "us-east-1"
STANDARD = Ladder.builtin("standard")
ENVIRONMENT = {
    "FAST_PREFIX": "fast",
    "LIBRARY_TABLE": "library",
    "MEDIA_BUCKET": "media",
    "INGEST_BUCKET": "ingest",
    "EVENT_BUS": "fast-channels",
    "INGEST_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:fast-ingest",
    "PROVISION_STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:fast-ladder-provisioner",
    "SOURCE_LOCATION": "fast-origin",
    "CONTENT_BASE_URL": "https://media.example.com",
    "PACKAGE_PROFILES": "hls,hls-avc,hls-hevc,hls-av1,dash-avc",
    "DEFAULT_LADDER_VERSION": STANDARD.version,
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_DEFAULT_REGION": REGION,
}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Lambda environment of the ingest functions (settings are re-read per test)."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    yield
    settings.cache_clear()


class Stubs:
    """Stubbed clients behind ``aws.client`` and ``aws.table``; ``stubs["s3"]`` is the S3 Stubber."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._clients: dict[str, Any] = {}
        self._stubbers: dict[str, Stubber] = {}
        self._dynamodb = boto3.resource("dynamodb", region_name=REGION)
        self._stubbers["dynamodb"] = Stubber(self._dynamodb.meta.client)
        self._stubbers["dynamodb"].activate()
        monkeypatch.setattr(aws, "client", self.client)
        monkeypatch.setattr(aws, "table", self.table)

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = boto3.client(service, region_name=REGION)  # type: ignore[call-overload]
            self._stubbers[service] = Stubber(self._clients[service])
            self._stubbers[service].activate()
        return self._clients[service]

    def table(self, name: str) -> Any:
        return self._dynamodb.Table(name)

    def __getitem__(self, service: str) -> Stubber:
        if service != "dynamodb":
            self.client(service)
        return self._stubbers[service]

    def assert_done(self) -> None:
        for stubber in self._stubbers.values():
            stubber.assert_no_pending_responses()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubs]:
    """Stubbed AWS clients; every queued response must be consumed by the test."""
    stubbed = Stubs(monkeypatch)
    yield stubbed
    stubbed.assert_done()


class PutEvents:
    """Matches PutEvents ``Entries`` holding one event of ``detail_type`` whose detail includes ``fields``."""

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, detail_type: str, fields: dict[str, Any]) -> None:
        self.detail_type, self.fields = detail_type, fields

    def __eq__(self, entries: object) -> bool:
        [entry] = cast("list[dict[str, str]]", entries)
        detail = json.loads(entry["Detail"])
        return entry["DetailType"] == self.detail_type and all(
            detail[k] == v if not callable(v) else v(detail[k]) for k, v in self.fields.items()
        )


@pytest.fixture
def expect_event(stubs: Stubs) -> Callable[..., None]:
    """``expect_event(detail_type, **fields)``: queue a successful PutEvents of that event; a callable field value
    is a predicate on the detail value."""

    def expect(detail_type: str, **fields: Any) -> None:
        response = {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}
        stubs["events"].add_response("put_events", response, {"Entries": PutEvents(detail_type, fields)})

    return expect


class MemoryStore:
    """In-memory :class:`fast_channels.functions.ingest.storage.Store` that records its writes."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})
        self.writes: dict[str, bytes] = {}
        self.cache_control: dict[str, str | None] = {}

    def read(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def exists(self, key: str) -> bool:
        return key in self.objects

    def write(self, key: str, body: bytes, *, cache_control: str | None = None) -> None:
        self.objects[key] = body
        self.writes[key] = body
        self.cache_control[key] = cache_control


@pytest.fixture
def store() -> type[MemoryStore]:
    """The in-memory store class (``store(objects)``)."""
    return MemoryStore


def _encode(sample: str, asset_id: str, ladder: Ladder = STANDARD) -> tuple[str, dict[str, bytes]]:
    prefix = f"assets/{asset_id}/{ladder_path(ladder.version)}/"
    objects = {f"{prefix}{path.name}": path.read_bytes() for path in (DATA / sample).glob("*.m3u8")}
    mpd = (DATA / "main_avc.mpd").read_bytes()
    objects |= {f"{prefix}main_{codec}.mpd": mpd for codec in ("avc", "hevc", "av1")}
    for rendition in (r for r in ladder.video if r.codec == "av1"):
        init = DATA / "init" / f"standard-av1-{'1080p' if rendition.height == 1080 else '360p'}-2997init.cmfv"
        objects[f"{prefix}main_{rendition.name}init.cmfv"] = init.read_bytes()
    return prefix, objects


@pytest.fixture
def encode() -> Any:
    """``encode(sample, asset_id) -> (prefix, objects)``: a STANDARD-ladder encode under
    ``assets/<asset_id>/<ladder>/`` made of the real HLS manifests of ``data/<sample>``, a real MediaConvert
    per-codec MPD for each DASH profile and real AV1 init segments."""
    return _encode


@pytest.fixture
def lambda_context() -> Any:
    """A minimal LambdaContext for Powertools' ``inject_lambda_context``."""
    return SimpleNamespace(
        function_name="test",
        function_version="$LATEST",
        invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:test",
        memory_limit_in_mb=512,
        aws_request_id="request-1",
        log_group_name="/aws/lambda/test",
        log_stream_name="stream",
        client_context=None,
    )
