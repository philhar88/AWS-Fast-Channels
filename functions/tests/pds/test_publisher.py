"""The snapshot publisher: ip-ranges.json in, a snapshot out only when its content changed."""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot, SnapshotBase, build, demo_demand
from fast_channels.functions import aws
from fast_channels.functions.pds import publisher
from fast_channels.functions.settings import settings

BUCKET = "fastm-pds-config"
BASE = SnapshotBase(demand=demo_demand("fastm"))
NOW = datetime(2026, 10, 29, 2, 3, 4, tzinfo=UTC)
RANGES: dict[str, Any] = {
    "syncToken": "1793278000",
    "prefixes": [
        {"ip_prefix": "130.176.0.0/18", "service": "CLOUDFRONT_ORIGIN_FACING"},
        {"ip_prefix": "3.80.0.0/12", "service": "EC2"},
    ],
    "ipv6_prefixes": [],
}


@dataclass
class Context:
    function_name: str = "fastm-pds-publisher"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-pds-publisher"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-pds-publisher"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 60_000


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("PDS_CONFIG_BUCKET", BUCKET)
    monkeypatch.setenv("PDS_SNAPSHOT_BASE", BASE.model_dump_json())
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def s3() -> Iterator[Stubber]:
    with Stubber(aws.client("s3")) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def serve(monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception) -> list[str]:
    calls: list[str] = []

    def get(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(publisher.httpx, "get", get)
    return calls


def ranges(document: Any = RANGES, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=document, request=httpx.Request("GET", publisher.IP_RANGES_URL))


def published(s3: Stubber, snapshot: Snapshot | None) -> None:
    if snapshot is None:
        s3.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        return
    text = snapshot.model_dump_json().encode()
    s3.add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(text), len(text))}, {"Bucket": BUCKET, "Key": SNAPSHOT_KEY}
    )


def written(s3: Stubber) -> None:
    for key in (ANY, SNAPSHOT_KEY):
        s3.add_response(
            "put_object", {}, {"Bucket": BUCKET, "Key": key, "Body": ANY, "ContentType": "application/json"}
        )


def test_the_first_publish_writes_the_snapshot_and_its_history(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = serve(monkeypatch, ranges())
    published(s3, None)
    written(s3)

    result = publisher.handler({}, Context())

    assert calls == [publisher.IP_RANGES_URL]
    assert result["written"] is True
    assert (result["origin_facing"], result["bidders"]) == (1, ["amt"])


def test_an_unchanged_snapshot_is_not_written_again(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, ranges())
    published(s3, build(BASE, RANGES, now=datetime(2026, 10, 1, tzinfo=UTC)))

    snapshot, was_written = publisher.publish(BUCKET, BASE, now=NOW)

    assert not was_written
    assert snapshot.version.startswith("20261001T")


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectTimeout("slow"),
        ranges(status=503),
        ranges({"syncToken": "2", "prefixes": [{"ip_prefix": "3.80.0.0/12", "service": "EC2"}]}),
    ],
    ids=["timeout", "error", "no-origin-facing"],
)
def test_without_ip_ranges_the_published_networks_are_kept(
    s3: Stubber, monkeypatch: pytest.MonkeyPatch, answer: httpx.Response | Exception
) -> None:
    serve(monkeypatch, answer)
    before = build(BASE, RANGES, now=datetime(2026, 10, 1, tzinfo=UTC))
    published(s3, before)
    written(s3)

    snapshot, was_written = publisher.publish(BUCKET, BASE.model_copy(update={"shed": frozenset({"amt"})}), now=NOW)

    assert was_written
    assert (snapshot.origin_facing, snapshot.ip_ranges, snapshot.shed) == (
        before.origin_facing,
        before.ip_ranges,
        frozenset({"amt"}),
    )


def test_an_invalid_published_snapshot_is_replaced(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, ranges())
    s3.add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(b"{}"), 2)}, {"Bucket": BUCKET, "Key": SNAPSHOT_KEY}
    )
    written(s3)

    assert publisher.publish(BUCKET, BASE, now=NOW)[1] is True


def test_other_read_errors_fail_the_run(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, ranges())
    s3.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)

    with pytest.raises(Exception, match="AccessDenied"):
        publisher.publish(BUCKET, BASE, now=NOW)


def test_the_published_body_is_the_snapshot(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, ranges())
    published(s3, None)
    bodies: list[bytes] = []

    def capture(**kwargs: Any) -> None:
        bodies.append(kwargs["Body"])

    s3.add_response("put_object", {}, {"Bucket": BUCKET, "Key": ANY, "Body": ANY, "ContentType": "application/json"})
    s3.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": SNAPSHOT_KEY, "Body": ANY, "ContentType": "application/json"}
    )
    aws.client("s3").meta.events.register("before-parameter-build.s3.PutObject", lambda params, **_: capture(**params))

    snapshot, _ = publisher.publish(BUCKET, BASE, now=NOW)

    assert [Snapshot.model_validate(json.loads(b)) for b in bodies] == [snapshot, snapshot]
