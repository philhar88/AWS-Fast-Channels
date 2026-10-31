"""The snapshot publisher: ip-ranges.json and the default ladder's kit in, a snapshot out only when its content
changed, and the lines' budgets set."""

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

from fast_channels.core.demo_demand import DEMO_CREATIVES, demo_index, demo_index_key
from fast_channels.core.pds.book import Book, Line
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, Snapshot, SnapshotBase, build, demo_demand, from_ip_ranges
from fast_channels.core.specs import Creative as KitCreative
from fast_channels.core.specs import vast_key, vast_pod
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
MEDIA = "fastm-media"
LADDER = "standard@843ed1d05a"
CDN = "https://d111111abcdef8.cloudfront.net"
LINE = Line(
    id="direct-1",
    advertiser="Direct Advertiser 1",
    adomain="direct1.example",
    creatives=("direct1",),
    cpm=8,
    budget=100,
    tier0=True,
)
LINED = BASE.model_copy(update={"lines": (LINE,)})
POD = vast_pod(
    creatives=[
        KitCreative(creative_id=f"house-ad-{n}-x", duration_ms=30030, hls_uri=f"{CDN}/ads/s/house-ad-{n}-x/main.m3u8")
        for n in (1, 2)
    ],
    content_base_url=CDN,
    fill_ms=60060,
)
INDEX = demo_index(
    LADDER,
    {c.crid: (f"demo-{c.crid}", f"{CDN}/ads/s/demo-{c.crid}/main.m3u8", 30030) for c in DEMO_CREATIVES},
    width=1920,
    height=1080,
)


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
    monkeypatch.setenv("PDS_SNAPSHOT_BASE", LINED.model_dump_json())
    monkeypatch.setenv("FAST_PREFIX", "fastm")
    monkeypatch.setenv("MEDIA_BUCKET", MEDIA)
    monkeypatch.setenv("PDS_TABLE", "fastm-pds")
    monkeypatch.setenv("LEDGER_TABLE", "fastm-ledger")
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


@pytest.fixture
def ssm() -> Iterator[Stubber]:
    with Stubber(aws.client("ssm")) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def body(data: bytes) -> dict[str, Any]:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def kit(s3: Stubber, ssm: Stubber, *, demo: bool = True) -> None:
    ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": "/fastm/pds/ladder-version", "Value": LADDER}},
        {"Name": "/fastm/pds/ladder-version"},
    )
    s3.add_response("get_object", body(POD.encode()), {"Bucket": MEDIA, "Key": vast_key(LADDER)})
    if demo:
        s3.add_response(
            "get_object", body(INDEX.model_dump_json().encode()), {"Bucket": MEDIA, "Key": demo_index_key(LADDER)}
        )


def test_the_first_publish_writes_the_snapshot_and_its_history(
    s3: Stubber, ssm: Stubber, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = serve(monkeypatch, ranges())
    budgets: list[dict[str, Any]] = []
    monkeypatch.setattr(publisher, "set_budgets", lambda client, **kwargs: budgets.append(kwargs) or {"direct-1": [0]})
    published(s3, None)
    kit(s3, ssm)
    written(s3)

    result = publisher.handler({}, Context())

    assert calls == [publisher.IP_RANGES_URL]
    assert result["written"] is True
    assert (result["origin_facing"], result["bidders"]) == (1, ["amt"])
    assert (result["ladder"], result["house"], result["lines"]) == (
        LADDER,
        ["house1", "house2"],
        {"direct-1": ["direct1"]},
    )
    assert result["budgets_set"] == {"direct-1": [0]}
    [call] = budgets
    assert (call["table"], call["ledger_table"], call["lines"]) == ("fastm-pds", "fastm-ledger", (LINE,))


def test_the_book_is_the_default_ladders_kit(s3: Stubber, ssm: Stubber) -> None:
    kit(s3, ssm)

    own = publisher.own_book(LINED, None, prefix="fastm", media_bucket=MEDIA)

    assert own.ladder_version == LADDER
    assert [c.crid for c in own.house] == ["house1", "house2"]
    assert [(c.crid, c.advertiser) for c in own.creatives] == [("direct1", "Direct Advertiser 1")]
    assert own.lines == (LINE,)


def test_without_lines_the_demo_index_is_not_read(s3: Stubber, ssm: Stubber) -> None:
    kit(s3, ssm, demo=False)

    own = publisher.own_book(BASE, None, prefix="fastm", media_bucket=MEDIA)

    assert (own.lines, own.creatives, len(own.house)) == ((), (), 2)


@pytest.mark.parametrize(("code", "status"), [("NoSuchKey", 404), ("AccessDenied", 403)])
def test_a_kit_without_demo_creatives_bids_house_only(s3: Stubber, ssm: Stubber, code: str, status: int) -> None:
    """Without s3:ListBucket, S3 answers a missing key with AccessDenied."""
    kit(s3, ssm, demo=False)
    s3.add_client_error("get_object", service_error_code=code, http_status_code=status)

    own = publisher.own_book(LINED, None, prefix="fastm", media_bucket=MEDIA)

    assert (own.ladder_version, own.creatives, len(own.house)) == (LADDER, (), 2)


@pytest.mark.parametrize("failure", ["parameter", "pod", "invalid-pod", "index-error"])
def test_an_unreadable_kit_keeps_the_published_creatives(s3: Stubber, ssm: Stubber, failure: str) -> None:
    kept = Book(ladder_version="standard@0000000000", house=(), creatives=())
    snapshot = build(LINED, own=kept, now=NOW)
    match failure:
        case "parameter":
            ssm.add_client_error("get_parameter", service_error_code="ParameterNotFound", http_status_code=400)
        case "pod" | "invalid-pod" | "index-error":
            ssm.add_response("get_parameter", {"Parameter": {"Name": "/fastm/pds/ladder-version", "Value": LADDER}})
            if failure == "pod":
                s3.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
            elif failure == "invalid-pod":
                s3.add_response("get_object", body(b"<VAST"))
            else:
                s3.add_response("get_object", body(POD.encode()))
                s3.add_client_error("get_object", service_error_code="InternalError", http_status_code=500)

    own = publisher.own_book(LINED, snapshot, prefix="fastm", media_bucket=MEDIA)

    assert (own.ladder_version, own.lines) == ("standard@0000000000", (LINE,))


def test_without_a_kit_or_a_published_book_only_the_lines_are_known(ssm: Stubber) -> None:
    ssm.add_client_error("get_parameter", service_error_code="ParameterNotFound", http_status_code=400)

    assert publisher.own_book(LINED, None, prefix="fastm", media_bucket=MEDIA) == Book(lines=(LINE,))


def test_an_unchanged_snapshot_is_not_written_again(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, ranges())
    published(
        s3,
        build(
            BASE,
            origin_facing=from_ip_ranges(RANGES)[0],
            ip_ranges=from_ip_ranges(RANGES)[1],
            now=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    )

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
    before = build(
        BASE,
        origin_facing=from_ip_ranges(RANGES)[0],
        ip_ranges=from_ip_ranges(RANGES)[1],
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    published(s3, before)
    written(s3)

    snapshot, was_written = publisher.publish(BUCKET, BASE.model_copy(update={"shed": frozenset({"amt"})}), now=NOW)

    assert was_written
    assert (snapshot.origin_facing, snapshot.ip_ranges, snapshot.shed) == (
        before.origin_facing,
        before.ip_ranges,
        frozenset({"amt"}),
    )


def test_a_first_publish_without_ip_ranges_has_no_network(s3: Stubber, monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, httpx.ConnectTimeout("slow"))
    published(s3, None)
    written(s3)

    snapshot, was_written = publisher.publish(BUCKET, BASE, now=NOW)

    assert was_written and snapshot.origin_facing == () and snapshot.ip_ranges is None


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

    def handler(params: dict[str, Any], **_: Any) -> None:
        capture(**params)

    events = aws.client("s3").meta.events
    events.register("before-parameter-build.s3.PutObject", handler)
    try:
        snapshot, _ = publisher.publish(BUCKET, BASE, now=NOW)
    finally:
        events.unregister("before-parameter-build.s3.PutObject", handler)

    assert [Snapshot.model_validate(json.loads(b)) for b in bodies] == [snapshot, snapshot]
