"""Content enrichment (``modules.crawl``): the catalog context publisher on a deployed stage.

Runs only when the stage has the Crawl stack (for example ``STAGE=fastci``); the v3.0.0 leg skips it. The test
writes one ``ReferenceCatalog`` item of its own, puts ``Catalog Item Changed`` events on the bus and reads the
catalog-context bucket and the CDN. It never touches the ingest bucket, and it removes its item and object at the end.
"""

import json
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Final

import httpx
import pytest
from _stack import Plane, Stage, stage_id
from botocore.exceptions import ClientError
from harness import wait_for

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CACHE_CONTROL, context_key
from fast_channels.core.events import CatalogItemChanged, put_events_entry
from fast_channels.functions.tools.catalog import catalog_item

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

pytestmark = pytest.mark.e2e

PUBLISH_TIMEOUT_S: Final = 60
"""Acceptance (BUILD-PLAN M17-02): a Catalog Item Changed event rewrites the object within a minute."""


@pytest.fixture(scope="module")
def crawl(stage: Stage) -> dict[str, str]:
    """Outputs of the stage's Crawl stack; skips the module when the stage has none."""
    if not (outputs := crawl_outputs(stage)):
        pytest.skip(f"stage {stage.prefix} has no {stage_id(stage.prefix)}-Crawl (modules.crawl is off)")
    return outputs


@pytest.fixture(scope="module")
def title(run_id: str) -> ContentItem:
    """The test's own catalog title (its id is the run id plus ``-ctx``)."""
    return ContentItem(
        content_id=f"{run_id}-ctx",
        title="E2E crawl: a title, with punctuation & more",
        rating="TV-G",
        genres=["documentary", "e2e-only"],
        duration_ms=61_000,
        language="en",
        prodq=1,
    )


@pytest.fixture(scope="module")
def catalog(stage: Stage, crawl: dict[str, str], title: ContentItem) -> Iterator[Table]:
    """The stage's ReferenceCatalog table; the test's item and object are removed afterwards."""
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Plane.DATA, "CatalogTableName")
    )
    yield table
    table.delete_item(Key={"pk": f"CONTENT#{title.content_id}", "sk": "META"})
    s3 = stage.session.client("s3", region_name=stage.region)
    s3.delete_object(Bucket=crawl["CatalogContextBucketName"], Key=context_key(title.content_id))


def announce(stage: Stage, content_id: str, run_id: str) -> float:
    """Put a ``Catalog Item Changed`` event for ``content_id``; returns when it was put."""
    events = stage.session.client("events", region_name=stage.region)
    entry = put_events_entry(
        CatalogItemChanged(content_id=content_id, correlation_id=run_id),
        prefix=stage.prefix,
        event_bus=stage.output(Plane.MEDIA, "EventBusName"),
    )
    response = events.put_events(Entries=[entry])  # type: ignore[list-item]  # put_events_entry builds a plain dict
    assert response["FailedEntryCount"] == 0, response
    return time.monotonic()


def crawl_outputs(stage: Stage) -> dict[str, str]:
    """The Crawl stack's outputs, or {} when the stage has no Crawl stack."""
    try:
        [stack] = stage.cloudformation.describe_stacks(StackName=f"{stage_id(stage.prefix)}-Crawl")["Stacks"]
    except ClientError as error:
        if "does not exist" in error.response.get("Error", {}).get("Message", ""):
            return {}
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def head(stage: Stage, content_id: str) -> dict[str, Any] | None:
    """The object's HEAD in the catalog-context bucket, or None while it does not exist."""
    s3 = stage.session.client("s3", region_name=stage.region)
    bucket = crawl_outputs(stage)["CatalogContextBucketName"]
    try:
        return dict(s3.head_object(Bucket=bucket, Key=context_key(content_id)))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def changed(stage: Stage, content_id: str, digest: str) -> bool | None:
    """True once the object's digest differs from ``digest``."""
    found = head(stage, content_id)
    return True if found and found["Metadata"]["digest"] != digest else None


def gone(stage: Stage, content_id: str) -> bool | None:
    """True once the object no longer exists."""
    return True if head(stage, content_id) is None else None


def test_a_changed_title_is_published_rewritten_and_removed(
    stage: Stage, crawl: dict[str, str], catalog: Table, title: ContentItem, run_id: str
) -> None:
    content_id = title.content_id
    catalog.put_item(Item=catalog_item(title))
    sent = announce(stage, content_id, run_id)
    first = wait_for(lambda: head(stage, content_id), timeout=PUBLISH_TIMEOUT_S, interval=2, what="the object")
    print(f"published {time.monotonic() - sent:.1f} s after the event")
    assert first["CacheControl"] == CACHE_CONTROL and first["ContentType"] == "application/json"

    response = httpx.get(f"{crawl['CatalogContextBaseUrl']}{content_id}.json", timeout=10)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == CACHE_CONTROL
    document = response.json()
    assert (document["v"], document["id"], document["genres"], document["cat"]) == (1, content_id, ["332"], ["332"])
    assert (document["gtax"], document["cattax"], document["genre"], document["kwarray"]) == (
        9,
        9,
        "Documentary",
        ["e2e-only"],
    )
    assert document["title_q"] == "E2E%20crawl%3A%20a%20title%2C%20with%20punctuation%20%26%20more"
    assert document["hdr"] == f"v1;id={content_id};g=332;c=332;r=TV-G;l=61;la=en;pq=1"

    catalog.put_item(Item=catalog_item(title.model_copy(update={"keywords": ["rewritten"]})))
    sent = announce(stage, content_id, run_id)
    digest = first["Metadata"]["digest"]
    wait_for(lambda: changed(stage, content_id, digest), timeout=PUBLISH_TIMEOUT_S, interval=2, what="the rewrite")
    print(f"rewritten {time.monotonic() - sent:.1f} s after the event")

    catalog.delete_item(Key={"pk": f"CONTENT#{content_id}", "sk": "META"})
    announce(stage, content_id, run_id)
    wait_for(lambda: gone(stage, content_id), timeout=PUBLISH_TIMEOUT_S, interval=2, what="the deletion")


def test_the_sweep_runs_with_its_own_permissions(stage: Stage, crawl: dict[str, str]) -> None:
    client = stage.session.client("lambda", region_name=stage.region)
    response = client.invoke(FunctionName=crawl["CatalogContextPublisherName"], Payload=json.dumps({"sweep": True}))
    result = json.loads(response["Payload"].read())
    assert "FunctionError" not in response, result
    assert set(result) == {"written", "unchanged", "deleted", "invalid"}
    assert result["invalid"] == 0


def test_a_missing_title_is_a_404_and_the_route_never_lists(crawl: dict[str, str], run_id: str) -> None:
    """MediaTailor's Function cache stores a 404 but not a 403, so the catalog-context bucket lets CloudFront list
    it; the route still forwards neither the bucket root nor a query string, so no listing can come back."""
    base = crawl["CatalogContextBaseUrl"]  # https://<cdn>/catalog/ctx/v1/
    missing = httpx.get(f"{base}{run_id}-missing.json", timeout=10)
    assert missing.status_code == 404, (missing.status_code, missing.text[:200])
    root = base.removesuffix("v1/")
    for url in (base, root, f"{root}?list-type=2", f"{base}?list-type=2&prefix=catalog/", f"{root}?delimiter=/"):
        response = httpx.get(url, timeout=10)
        assert response.status_code == 404 and "ListBucketResult" not in response.text, (url, response.status_code)
