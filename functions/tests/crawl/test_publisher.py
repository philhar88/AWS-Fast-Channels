"""The catalog context publisher: each trigger, the digest check, removals and invalid items."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CatalogContext, build_context, digest
from fast_channels.core.crawl.taxonomy import default_genre_map
from fast_channels.functions import aws
from fast_channels.functions.crawl import publisher
from fast_channels.functions.settings import settings
from fast_channels.functions.tools.catalog import catalog_item

CATALOG, LIBRARY, BUCKET = "catalog-test", "library-test", "catalog-contexts-test"
PUBLISHER = "example.com"
SINTEL = ContentItem(
    content_id="sintel",
    title="Sintel",
    rating="TV-14",
    genres=["animation", "fantasy", "adventure"],
    duration_ms=888_000,
    language="en",
)
NOW = datetime(2026, 10, 29, 1, 2, 3, tzinfo=UTC)
_serialize = TypeSerializer().serialize


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _Frozen:
        return cls.fromtimestamp(NOW.timestamp(), tz)


@dataclass
class Context:
    function_name: str = "publisher"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:publisher"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/publisher"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("CATALOG_TABLE", CATALOG)
    monkeypatch.setenv("LIBRARY_TABLE", LIBRARY)
    monkeypatch.setenv("CATALOG_CONTEXT_BUCKET", BUCKET)
    monkeypatch.setenv("PUBLISHER_DOMAIN", PUBLISHER)
    monkeypatch.setattr(publisher, "datetime", _Frozen)
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def s3() -> Iterator[Stubber]:
    with Stubber(aws.client("s3")) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def catalog() -> Iterator[Stubber]:
    with Stubber(aws.table(CATALOG).meta.client) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def library() -> Iterator[Stubber]:
    with Stubber(aws.table(LIBRARY).meta.client) as stub:
        yield stub
        stub.assert_no_pending_responses()


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


def handle(event: dict[str, Any]) -> dict[str, int]:
    return publisher.handler(event, cast("Any", Context()))


def expect_get(catalog: Stubber, item: ContentItem | None, content_id: str) -> None:
    response = {"Item": dynamo(catalog_item(item))} if item else {}
    catalog.add_response(
        "get_item", response, {"TableName": CATALOG, "Key": {"pk": f"CONTENT#{content_id}", "sk": "META"}}
    )


def expect_head(s3: Stubber, key: str, current: str | None, *, error: str = "404") -> None:
    if current is None:
        s3.add_client_error("head_object", service_error_code=error, http_status_code=int(error))
    else:
        s3.add_response("head_object", {"Metadata": {"digest": current}}, {"Bucket": BUCKET, "Key": key})


def built(item: ContentItem, domain: str | None = PUBLISHER) -> CatalogContext:
    return build_context(item, genre_map=default_genre_map(), publisher_domain=domain, updated=NOW)


def digest_of(item: ContentItem, domain: str | None = PUBLISHER) -> str:
    return digest(built(item, domain))


def expect_put(s3: Stubber, item: ContentItem) -> None:
    """Queue the write of ``item``'s object, exactly as the publisher must make it."""
    expected = {
        "Bucket": BUCKET,
        "Key": f"catalog/ctx/v1/{item.content_id}.json",
        "Body": built(item).model_dump_json().encode(),
        "ContentType": "application/json",
        "CacheControl": "max-age=300",
        "Metadata": {"digest": digest_of(item)},
    }
    s3.add_response("put_object", {}, expected)


def changed_event(content_id: str, correlation_id: str | None = "cms-1") -> dict[str, Any]:
    detail = {"content_id": content_id} | ({"correlation_id": correlation_id} if correlation_id else {})
    return {"id": "event-1", "detail-type": "Catalog Item Changed", "source": "fast-channels.fastm", "detail": detail}


def test_a_changed_title_is_written_with_its_digest_and_caching(s3: Stubber, catalog: Stubber) -> None:
    expect_get(catalog, SINTEL, "sintel")
    expect_head(s3, "catalog/ctx/v1/sintel.json", None)
    expect_put(s3, SINTEL)
    assert handle(changed_event("sintel")) == {"written": 1, "unchanged": 0, "deleted": 0, "invalid": 0}
    document = json.loads(built(SINTEL).model_dump_json())  # what was written
    assert document["genres"] == ["641", "335", "325"] and document["updated"] == "2026-10-29T01:02:03Z"
    assert document["cids"] == [{"name": PUBLISHER, "ids": ["sintel"]}]
    assert document["hdr"] == "v1;id=sintel;g=641,335,325;c=641,335,325;r=TV-14;l=888;la=en"


def test_an_unchanged_title_is_not_rewritten(s3: Stubber, catalog: Stubber) -> None:
    expect_get(catalog, SINTEL, "sintel")
    expect_head(s3, "catalog/ctx/v1/sintel.json", digest_of(SINTEL))
    assert handle(changed_event("sintel", correlation_id=None))["unchanged"] == 1


def test_a_title_that_left_the_catalog_loses_its_object(s3: Stubber, catalog: Stubber) -> None:
    expect_get(catalog, None, "gone")
    s3.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": "catalog/ctx/v1/gone.json"})
    assert handle(changed_event("gone"))["deleted"] == 1


def test_an_invalid_catalog_item_is_reported_and_left_alone(catalog: Stubber) -> None:
    bad = {"pk": "CONTENT#bad", "sk": "META", "content_id": "bad", "title": "Bad", "language": "English"}
    catalog.add_response(
        "get_item", {"Item": dynamo(bad)}, {"TableName": CATALOG, "Key": {"pk": "CONTENT#bad", "sk": "META"}}
    )
    assert handle(changed_event("bad"))["invalid"] == 1


def test_without_a_publisher_domain_the_object_has_no_publisher_ids(
    s3: Stubber, catalog: Stubber, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLISHER_DOMAIN")
    settings.cache_clear()
    expect_get(catalog, SINTEL, "sintel")
    expect_head(s3, "catalog/ctx/v1/sintel.json", digest_of(SINTEL, domain=None))
    assert handle(changed_event("sintel"))["unchanged"] == 1


def test_a_published_plan_publishes_each_of_its_titles_once(s3: Stubber, catalog: Stubber, library: Stubber) -> None:
    def program(content_id: str | None, plan_id: str) -> dict[str, Any]:
        return dynamo({"plan_id": plan_id} | ({"content_id": content_id} if content_id else {}))

    items = [
        program("sintel", "plan-1"),
        program("sintel", "plan-1"),  # the same title twice
        program(None, "plan-1"),  # an asset without a catalog title
        program("other", "plan-0"),  # an earlier plan in the window
    ]
    library.add_response(
        "query",
        {"Items": items},
        {"TableName": LIBRARY, "KeyConditionExpression": ANY, "ProjectionExpression": "content_id, plan_id"},
    )
    expect_get(catalog, SINTEL, "sintel")
    expect_head(s3, "catalog/ctx/v1/sintel.json", None, error="403")
    expect_put(s3, SINTEL)
    detail = {
        "channel": "fastm-demo",
        "plan_id": "plan-1",
        "programs": 3,
        "start_ms": 1000,
        "end_ms": 4000,
        "correlation_id": "session-1",
    }
    assert handle({"detail-type": "Schedule Published", "detail": detail})["written"] == 1


def test_the_sweep_publishes_the_catalog_and_removes_orphans(s3: Stubber, catalog: Stubber) -> None:
    tears = SINTEL.model_copy(update={"content_id": "tears-of-steel", "title": "Tears of Steel"})
    catalog.add_response(
        "scan",
        {"Items": [dynamo(catalog_item(SINTEL))], "LastEvaluatedKey": {"pk": _serialize("CONTENT#sintel")}},
        {"TableName": CATALOG, "FilterExpression": "sk = :meta", "ExpressionAttributeValues": {":meta": "META"}},
    )
    catalog.add_response("scan", {"Items": [dynamo(catalog_item(tears))]})
    expect_head(s3, "catalog/ctx/v1/sintel.json", digest_of(SINTEL))
    expect_head(s3, "catalog/ctx/v1/tears-of-steel.json", None)
    expect_put(s3, tears)
    keys = ["sintel", "tears-of-steel", "removed", "nested/x"]
    s3.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": f"catalog/ctx/v1/{k}.json"} for k in keys] + [{"Key": "catalog/ctx/v1/notes.txt"}]},
        {"Bucket": BUCKET, "Prefix": "catalog/ctx/v1/"},
    )
    s3.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": "catalog/ctx/v1/removed.json"})
    assert handle({"sweep": True}) == {"written": 1, "unchanged": 1, "deleted": 1, "invalid": 0}


def test_other_errors_are_raised_for_a_retry(s3: Stubber, catalog: Stubber) -> None:
    expect_get(catalog, SINTEL, "sintel")
    s3.add_client_error("head_object", service_error_code="500", http_status_code=500)
    with pytest.raises(Exception, match="500"):
        handle(changed_event("sintel"))


def test_unknown_events_are_refused() -> None:
    with pytest.raises(ValueError, match="not a publisher event"):
        handle({"detail-type": "Asset Ready", "detail": {}})
