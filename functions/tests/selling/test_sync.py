"""The selling module's sync (M7): the bridge's buys in, booked lines and delivery out."""

import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fast_channels.core.pds.book import Line
from fast_channels.core.pds.snapshot import PublishedLines
from fast_channels.core.selling.products import Terms
from fast_channels.functions import aws
from fast_channels.functions.selling import sync
from fast_channels.functions.settings import settings

BUCKET, KEY, TABLE, CATALOG, PUBLISHER = "fastm-pds-config", "lines/booked.json", "fastm-pds", "catalog", "publisher"
NOW = datetime.now(UTC)
TERMS = Terms(
    channels=("fastm-demo",),
    breaks_ms=(30030, 60060),
    cpm=Decimal(40),
    publisher_domain="fast-channels.example",
    creatives=("direct2",),
)
BUY: dict[str, Any] = {
    "media_buy_id": "buy_1",
    "status": "pending_creatives",
    "created_at": (NOW - timedelta(minutes=5)).isoformat(),
    "starts": (NOW - timedelta(minutes=5)).isoformat(),
    "ends": (NOW + timedelta(days=7)).isoformat(),
    "advertiser": "AWS reference buyer",
    "brand": "acme-outdoor.example",
    "packages": [{"package_id": "pkg_1", "product_id": "fastm-demo-midroll", "budget": "50", "cpm": "40"}],
}


@dataclass
class Context:
    function_name: str = "fastm-selling-sync"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-selling-sync"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-selling-sync"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 30_000


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "SELLING_PRODUCTS": TERMS.model_dump_json(),
        "CATALOG_TABLE": CATALOG,
        "PDS_CONFIG_BUCKET": BUCKET,
        "PDS_BOOKED_LINES_KEY": KEY,
        "PDS_TABLE": TABLE,
        "PDS_PUBLISHER_FUNCTION": PUBLISHER,
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def stubs() -> Iterator[dict[str, Stubber]]:
    stubbers = {service: Stubber(aws.client(service)) for service in ("s3", "dynamodb", "lambda")}
    for stubber in stubbers.values():
        stubber.activate()
    yield stubbers
    for stubber in stubbers.values():
        stubber.assert_no_pending_responses()
        stubber.deactivate()


def body(data: bytes) -> dict[str, Any]:
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def shards(stubs: dict[str, Stubber], delivered: dict[int, str]) -> None:
    stubs["dynamodb"].add_response(
        "batch_get_item",
        {
            "Responses": {
                TABLE: [
                    {"pk": {"S": "BUDGET#adcp-buy-1-1"}, "sk": {"S": f"S#{n}"}, "delivered": {"N": amount}}
                    for n, amount in delivered.items()
                ]
            }
        },
        {"RequestItems": ANY},
    )


def test_products_are_the_channels_with_the_catalogs_genres(stubs: dict[str, Stubber]) -> None:
    stubs["dynamodb"].add_response(
        "scan",
        {"Items": [{"genres": {"L": [{"S": "animation"}, {"S": "fantasy"}]}}, {"genres": {"L": [{"S": "sci-fi"}]}}]},
        {"TableName": CATALOG, "ProjectionExpression": "genres"},
    )

    [product] = sync.handler({"action": "products"}, Context())["products"]

    assert product["product_id"] == "fastm-demo-midroll" and product["cpm"] == "40"
    assert "genres: animation, fantasy, sci-fi." in product["description"]


def test_a_new_booking_publishes_its_line_and_runs_the_publisher(stubs: dict[str, Stubber]) -> None:
    stubs["s3"].add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    written: list[bytes] = []
    stubs["s3"].add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": KEY, "Body": ANY, "ContentType": "application/json"}
    )
    stubs["lambda"].add_response(
        "invoke", {"StatusCode": 202}, {"FunctionName": PUBLISHER, "InvocationType": "Event", "Payload": b"{}"}
    )
    shards(stubs, {0: "0.08", 2: "0.04"})
    events = aws.client("s3").meta.events

    def capture(params: dict[str, Any], **_: Any) -> None:
        written.append(params["Body"])

    events.register("before-parameter-build.s3.PutObject", capture)
    try:
        answer = sync.handler({"action": "sync", "buys": [BUY]}, Context())
    finally:
        events.unregister("before-parameter-build.s3.PutObject", capture)

    assert answer["changed"] is True
    assert answer["lines"] == {"adcp-buy-1-1": ["buy_1", "pkg_1"]}
    [line] = PublishedLines.model_validate_json(written[0]).lines
    assert (line.id, line.creatives, line.channels, line.cpm) == ("adcp-buy-1-1", ("direct2",), ("fastm-demo",), 40)
    [report] = answer["delivery"]
    assert (report["media_buy_id"], report["impressions"], report["spend"]) == ("buy_1", 3, "0.12")
    assert report["source"] == "seller server count, not MRC"


def test_unchanged_lines_are_not_published_again(stubs: dict[str, Stubber]) -> None:
    booked = sync.booked_lines(
        [sync.Buy.model_validate(BUY)], products=TERMS.products(()), pool=TERMS.creatives, now=NOW
    ).lines
    stubs["s3"].add_response(
        "get_object", body(PublishedLines(lines=booked).model_dump_json().encode()), {"Bucket": BUCKET, "Key": KEY}
    )
    shards(stubs, {})

    answer = sync.handler({"action": "sync", "buys": [BUY]}, Context())

    assert answer["changed"] is False
    assert [(r["impressions"], r["spend"]) for r in answer["delivery"]] == [(0, "0")]


def test_a_buy_that_ended_takes_its_line_out(stubs: dict[str, Stubber]) -> None:
    held = Line(
        id="adcp-buy-1-1",
        advertiser="x",
        adomain="acme-outdoor.example",
        creatives=("direct2",),
        cpm=Decimal(40),
        budget=Decimal(50),
    )
    stubs["s3"].add_response(
        "get_object", body(PublishedLines(lines=(held,)).model_dump_json().encode()), {"Bucket": BUCKET, "Key": KEY}
    )
    stubs["s3"].add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": KEY, "Body": ANY, "ContentType": "application/json"}
    )
    stubs["lambda"].add_response(
        "invoke", {"StatusCode": 202}, {"FunctionName": PUBLISHER, "InvocationType": "Event", "Payload": b"{}"}
    )

    ended = {**BUY, "ends": (NOW - timedelta(seconds=1)).isoformat()}
    answer = sync.handler({"action": "sync", "buys": [ended]}, Context())

    assert (answer["changed"], answer["lines"], answer["delivery"]) == (True, {}, [])


def test_the_request_is_checked() -> None:
    with pytest.raises(ValueError, match="action"):
        sync.handler({"action": "book"}, Context())
    assert json.loads(sync.Request(action="sync").model_dump_json()) == {"action": "sync", "buys": []}
