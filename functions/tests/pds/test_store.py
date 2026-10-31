"""The snapshot store: conditional refreshes every 30 s, the last good snapshot kept on any failure."""

import io
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from fast_channels.core.pds.snapshot import EMPTY, REFRESH_SECONDS, SNAPSHOT_KEY, SnapshotBase, build, demo_demand
from fast_channels.functions.pds.store import CLIENT_CONFIG, RETRY_SECONDS, SnapshotStore

BUCKET = "fastm-pds-config"
FIRST = build(SnapshotBase(demand=demo_demand("fastm")), now=datetime(2026, 10, 29, tzinfo=UTC))
SECOND = build(SnapshotBase(), now=datetime(2026, 10, 29, 1, tzinfo=UTC))


def inline(target: Callable[[], None]) -> None:
    target()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def s3() -> Iterator[tuple[Stubber, object]]:
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t")
    with Stubber(client) as stubber:
        yield stubber, client
        stubber.assert_no_pending_responses()


def body(text: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(text), len(text))


def test_loads_then_refreshes_conditionally(s3: tuple[Stubber, object]) -> None:
    stubber, client = s3
    clock = Clock()
    store = SnapshotStore(BUCKET, client=client, clock=clock, spawn=inline)
    stubber.add_response(
        "get_object",
        {"Body": body(FIRST.model_dump_json().encode()), "ETag": '"e1"'},
        {"Bucket": BUCKET, "Key": SNAPSHOT_KEY},
    )
    assert store.get() == FIRST
    clock.now += REFRESH_SECONDS - 1
    assert store.get() == FIRST, "no request before the refresh is due"

    clock.now += 1
    stubber.add_client_error(
        "get_object",
        service_error_code="304",
        http_status_code=304,
        expected_params={"Bucket": BUCKET, "Key": SNAPSHOT_KEY, "IfNoneMatch": '"e1"'},
    )
    assert store.get() == FIRST

    clock.now += REFRESH_SECONDS
    stubber.add_response(
        "get_object",
        {"Body": body(SECOND.model_dump_json().encode()), "ETag": '"e2"'},
        {"Bucket": BUCKET, "Key": SNAPSHOT_KEY, "IfNoneMatch": '"e1"'},
    )
    assert store.get() == SECOND


@pytest.mark.parametrize("failure", ["error", "invalid"])
def test_a_failed_refresh_keeps_what_it_has_and_retries_sooner(s3: tuple[Stubber, object], failure: str) -> None:
    stubber, client = s3
    clock = Clock()
    store = SnapshotStore(BUCKET, client=client, clock=clock, spawn=inline)
    if failure == "error":
        stubber.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
    else:
        stubber.add_response("get_object", {"Body": body(b'{"v": 9}'), "ETag": '"x"'})
    assert store.get() == EMPTY, "never loaded: the fail-safe snapshot"

    clock.now += RETRY_SECONDS
    stubber.add_response(
        "get_object",
        {"Body": body(FIRST.model_dump_json().encode()), "ETag": '"e1"'},
        {"Bucket": BUCKET, "Key": SNAPSHOT_KEY},
    )
    assert store.get() == FIRST


def test_the_default_client_has_short_timeouts() -> None:
    store = SnapshotStore(BUCKET)

    assert store.client.meta.config.read_timeout == CLIENT_CONFIG.read_timeout == 1
    assert store.client is store.client


def test_one_refresh_at_a_time_off_the_request_path(s3: tuple[Stubber, object]) -> None:
    stubber, client = s3
    pending: list[Callable[[], None]] = []
    store = SnapshotStore(BUCKET, client=client, clock=Clock(), spawn=pending.append)

    assert store.get() == EMPTY and store.get() == EMPTY, "the request never waits"
    assert len(pending) == 1, "a second request does not start a second refresh"
    stubber.add_response(
        "get_object",
        {"Body": body(FIRST.model_dump_json().encode()), "ETag": '"e1"'},
        {"Bucket": BUCKET, "Key": SNAPSHOT_KEY},
    )
    pending.pop()()
    assert store.get() == FIRST
    assert not pending, "not due yet"


def test_load_reads_now_and_the_default_spawn_is_a_thread(s3: tuple[Stubber, object]) -> None:
    stubber, client = s3
    clock = Clock()
    store = SnapshotStore(BUCKET, client=client, clock=clock)
    stubber.add_response(
        "get_object",
        {"Body": body(FIRST.model_dump_json().encode()), "ETag": '"e1"'},
        {"Bucket": BUCKET, "Key": SNAPSHOT_KEY},
    )
    assert store.load() == FIRST

    clock.now += REFRESH_SECONDS
    stubber.add_client_error("get_object", service_error_code="304", http_status_code=304)
    assert store.get() == FIRST
    for thread in threading.enumerate():
        if thread.name == "snapshot-refresh":
            thread.join(timeout=5)
    assert store.get() == FIRST
