"""Fixtures for the Release Controller's functions: the stage's environment, a bundle, an in-memory state and
ledger, and botocore Stubbers on the shared clients (any call the test did not expect fails it)."""

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from fast_channels.core.monetization.bundle import Bundle, ChannelInput, build_bundle, bundle_document
from fast_channels.core.monetization.validators import Violation, load_bundle
from fast_channels.core.pbs_config import POD_SECONDS, stored_request_id
from fast_channels.functions import aws
from fast_channels.functions.release import apply, store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from fast_channels.core.ledger import LedgerEntry
    from fast_channels.core.monetization.release import ChannelState, ReleaseStatus

PREFIX = "fastm"
CHANNEL = "fastm-demo"
CA_URL = "https://abc.channel-assembly.mediatailor.us-east-1.amazonaws.com/v1/channel/fastm-demo/"
CDN = "https://d111111abcdef8.cloudfront.net"
PBS = "https://d222222abcdef8.cloudfront.net"
SECRET_A, SECRET_B = "secret-value-a", "secret-value-b"
DESTINATION = "arn:aws:logs:us-east-1:123456789012:delivery-destination:fastm-release-mediatailor"
LAKE_ADS = "arn:aws:logs:us-east-1:123456789012:delivery-destination:fastm-lake-ads"
LAKE_MANIFEST = "arn:aws:logs:us-east-1:123456789012:delivery-destination:fastm-lake-manifest"
S3_LAYOUT = {"suffixPath": "{yyyy}/{MM}/{dd}/{HH}", "enableHiveCompatiblePath": False}
LAKE = {
    "AD_DECISION_SERVER_LOGS": {"destination": LAKE_ADS, "s3": S3_LAYOUT},
    "MANIFEST_SERVICE_LOGS": {"destination": LAKE_MANIFEST, "s3": S3_LAYOUT},
}


def make_bundle(**overrides: Any) -> Bundle:
    arguments: dict[str, Any] = {
        "prefix": PREFIX,
        "channels": [ChannelInput(name=CHANNEL, video_content_source_url=CA_URL)],
        "content_base_url": CDN,
        "pbs_origin": PBS,
        "log_percent": 100,
        "stored_requests": [stored_request_id(s, kids=k) for k in (False, True) for s in POD_SECONDS],
        "grid_seconds": POD_SECONDS,
    }
    return build_bundle(**(arguments | overrides))


@dataclass
class FakeStore:
    """The state table and the release bucket, in memory."""

    documents: dict[str, str] = field(default_factory=dict)
    statuses: dict[int, ReleaseStatus] = field(default_factory=dict)
    requests: dict[str, int] = field(default_factory=dict)
    states: dict[str, tuple[ChannelState, int]] = field(default_factory=dict)
    slot: str = "a"

    def add(self, bundle: Bundle) -> str:
        key = f"release/{bundle.digest}/bundle.json"
        self.documents[key] = json.dumps(bundle_document(bundle))
        return key

    def load(self, key: str) -> tuple[Bundle | None, list[Violation], str]:
        text = self.documents[key]
        found, violations = load_bundle(json.loads(text))
        return found, violations, text

    def bundle(self, key: str) -> Bundle:
        found = self.load(key)[0]
        assert found is not None
        return found

    def allocate(self, request: str) -> int:
        return self.requests.setdefault(request, len(self.requests) + 1)

    def status(self, release: int) -> ReleaseStatus | None:
        return self.statuses.get(release)

    def put_status(self, record: ReleaseStatus) -> None:
        self.statuses[record.release] = record

    def channel(self, name: str) -> tuple[ChannelState | None, int]:
        return self.states.get(name, (None, 0))

    def channels(self, names: list[str]) -> dict[str, ChannelState]:
        return {n: self.states[n][0] for n in names if n in self.states}

    def put_channel(self, state: ChannelState, *, version: int) -> None:
        assert self.channel(state.channel)[1] == version
        self.states[state.channel] = (state, version + 1)

    def origin_slot(self) -> str:
        return self.slot

    def secret(self, slot: str) -> SecretStr:
        return SecretStr({"a": SECRET_A, "b": SECRET_B}[slot])


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "FAST_PREFIX": PREFIX,
        "RELEASE_BUCKET": "fastm-release-bundles",
        "RELEASE_TABLE": "fastm-release",
        "LEDGER_TABLE": "fastm-ledger",
        "MEDIA_BUCKET": "fastm-media",
        "CDN_BASE_URL": CDN,
        "RELEASE_LOG_DESTINATION": DESTINATION,
        "LAKE_DELIVERIES": json.dumps(LAKE),
        "ORIGIN_SECRET_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(name, value)
    cached = store.bundle  # the state fixture replaces it before this teardown runs
    settings.cache_clear()
    cached.cache_clear()
    yield
    settings.cache_clear()
    cached.cache_clear()


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    """The store's functions, in memory, for the modules that use them."""
    fake = FakeStore()
    for name in (
        "load",
        "bundle",
        "allocate",
        "status",
        "put_status",
        "channel",
        "channels",
        "put_channel",
        "origin_slot",
        "secret",
    ):
        monkeypatch.setattr(store, name, getattr(fake, name))
    return fake


@dataclass
class Ledger:
    entries: list[LedgerEntry] = field(default_factory=list)

    def append(self, item: LedgerEntry, **_: Any) -> LedgerEntry:
        self.entries.append(item)
        return item

    def actions(self) -> list[tuple[str, str]]:
        return [(e.chain_id, e.action) for e in self.entries]


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> Ledger:
    fake = Ledger()
    monkeypatch.setattr(apply, "append", fake.append)
    return fake


SERVICES = ("dynamodb", "events", "logs", "mediatailor", "s3", "secretsmanager")


def _client(service: str) -> Any:
    match service:
        case "secretsmanager":
            return aws.regional_client("secretsmanager", "us-east-1")
        case "dynamodb":
            return aws.table("fastm-release").meta.client
        case _:
            return aws.client(service)


@pytest.fixture(autouse=True)
def stub() -> Iterator[Callable[[str], Stubber]]:
    """``stub("mediatailor")``: the active Stubber on a shared client. Every client the functions use is stubbed in
    every test, so a call the test did not queue fails it (none reaches AWS), and every queued one must be made."""
    stubbers = {service: Stubber(_client(service)) for service in SERVICES}
    for stubber in stubbers.values():
        stubber.activate()
    try:
        yield stubbers.__getitem__
    finally:  # the clients are shared: always detach the stubbers before asserting
        for stubber in stubbers.values():
            stubber.deactivate()
    for stubber in stubbers.values():
        stubber.assert_no_pending_responses()


@dataclass
class Context:
    function_name: str = "release"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:release"
    aws_request_id: str = "request-1"


@pytest.fixture
def context() -> Context:
    return Context()


@pytest.fixture
def kit() -> SimpleNamespace:
    return SimpleNamespace(
        PREFIX=PREFIX,
        CHANNEL=CHANNEL,
        CA_URL=CA_URL,
        CDN=CDN,
        PBS=PBS,
        SECRET_A=SECRET_A,
        DESTINATION=DESTINATION,
        LAKE_ADS=LAKE_ADS,
        LAKE_MANIFEST=LAKE_MANIFEST,
        S3_LAYOUT=S3_LAYOUT,
        make_bundle=make_bundle,
        at=datetime,
    )
