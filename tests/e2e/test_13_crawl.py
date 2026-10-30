"""Content enrichment (``modules.crawl``): the catalog context publisher and the PDS's ``app.content`` patch on a
deployed stage.

Runs only when the stage has the Crawl stack (for example ``STAGE=fastci``); the v3.0.0 leg skips it, and the patch
test needs the Pds stack too. The test writes one ``ReferenceCatalog`` item of its own, puts ``Catalog Item Changed``
events on the bus, reads the catalog-context bucket and the CDN, and sends two breaks of the title through the
guidance's edge as MediaTailor would (the PDS's sidecar answers ``/policy`` inside Prebid Server's task), then reads
their ``pds.policy`` decision records. It never touches the ingest bucket, and it removes its item and object at the
end.
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
from harness_demand import DemandEdge

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CACHE_CONTROL, context_key
from fast_channels.core.crawl.names import CONTRIBUTOR, RECORD_ARM, RECORD_SOURCE, Source
from fast_channels.core.demand import Parameter
from fast_channels.core.events import CatalogItemChanged, put_events_entry
from fast_channels.core.pds.buckets import bucket
from fast_channels.core.pds.names import CONTAINER
from fast_channels.core.timeline import UPID_FORMAT_ID
from fast_channels.functions.tools.catalog import catalog_item

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

pytestmark = pytest.mark.e2e

PUBLISH_TIMEOUT_S: Final = 60
"""Acceptance (BUILD-PLAN M17-02): a Catalog Item Changed event rewrites the object within a minute."""
COPY_TIMEOUT_S: Final = 120
"""The sidecar's copy of the catalog is refreshed every 30 s (and its startup reads it)."""
EVID: Final = "820461826"


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
    return module_outputs(stage, "Crawl")


def module_outputs(stage: Stage, stack: str) -> dict[str, str]:
    """The outputs of the stage's ``stack`` (``Crawl``, ``Pds``), or {} when the stage has none."""
    try:
        [described] = stage.cloudformation.describe_stacks(StackName=f"{stage_id(stage.prefix)}-{stack}")["Stacks"]
    except ClientError as error:
        if "does not exist" in error.response.get("Error", {}).get("Message", ""):
            return {}
        raise
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


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


def session_in(arm: int, run_id: str, percent: int) -> str:
    """A session id of the run whose break :data:`EVID` is in ``arm`` (1 enriched, 0 the ``crawl-v1`` holdout)."""
    for n in range(1000):
        sid = f"{run_id}-{n}"[-64:]
        if (bucket("crawl-v1", f"{sid}.{EVID}") >= percent) == bool(arm):
            return sid
    raise AssertionError("no such session")


def sidecar_environment(edge: DemandEdge, stage: Stage) -> dict[str, str]:
    """The PDS sidecar's environment, from the task definition the guidance's service runs."""
    ecs = stage.session.client("ecs", region_name=stage.region)
    cluster, service = edge.parameters[Parameter.CLUSTER_ARN.value], edge.parameters[Parameter.SERVICE_ARN.value]
    [described] = ecs.describe_services(cluster=cluster, services=[service])["services"]
    definition = ecs.describe_task_definition(taskDefinition=described["taskDefinition"])["taskDefinition"]
    [sidecar] = [c for c in definition["containerDefinitions"] if c["name"] == CONTAINER]
    return {e["name"]: e["value"] for e in sidecar["environment"]}


def policy_record(edge: DemandEdge, stage: Stage, group: str, sid: str, content_id: str) -> dict[str, Any] | None:
    """The ``pds.policy`` record of one contextual break of ``content_id`` sent through the guidance's edge, or None
    while it is not in the sidecar's log group."""
    since = int(time.time() * 1000) - 1000
    response = edge.auction(60, sid=sid, evid=EVID, upid=f"{UPID_FORMAT_ID}{content_id}", probe=False)
    assert response.status_code in {200, 204}, (response.status_code, response.text[:300])
    logs = stage.session.client("logs", region_name=stage.region)
    pattern = f'{{ ($.record = "pds.policy") && ($.sid = "{sid}") && ($.evid = "{EVID}") }}'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        events = logs.filter_log_events(logGroupName=group, startTime=since, filterPattern=pattern)["events"]
        if events:
            record: dict[str, Any] = json.loads(events[-1]["message"])
            return record
        time.sleep(3)
    return None


def test_the_pds_patches_app_content_on_the_enriched_arm_only(
    stage: Stage, crawl: dict[str, str], catalog: Table, title: ContentItem, run_id: str
) -> None:
    """The sidecar finds the title in its copy of the catalog and contributes on the enriched arm, and records the
    arm on both (``contributed.enr``, ``contributed.ctx``). The patch's fields are ``core.crawl.ortb_content``'s
    (``functions/tests/crawl``); the record never holds them."""
    if not (pds := module_outputs(stage, "Pds")):
        pytest.skip(f"stage {stage.prefix} has no {stage_id(stage.prefix)}-Pds (modules.pds is off)")
    edge = DemandEdge(stage.prefix, stage.session)
    variables = sidecar_environment(edge, stage)
    percent = int(variables["CRAWL_HOLDOUT_PERCENT"])
    catalog.put_item(Item=catalog_item(title))
    announce(stage, title.content_id, run_id)
    wait_for(lambda: head(stage, title.content_id), timeout=PUBLISH_TIMEOUT_S, interval=2, what="the object")
    enriched, held_out = session_in(1, run_id, percent), session_in(0, run_id, percent)
    group = pds["DecisionLogGroup"]

    def found() -> dict[str, Any] | None:
        record = policy_record(edge, stage, group, enriched, title.content_id)
        return record if record and record["contributed"].get(RECORD_SOURCE) == Source.SNAPSHOT.value else None

    record = wait_for(found, timeout=COPY_TIMEOUT_S, interval=5, what="the title in the sidecar's copy")
    assert CONTRIBUTOR in record["contributors"] and record["skipped"] == []
    assert record["contributed"] == {RECORD_ARM: 1, RECORD_SOURCE: Source.SNAPSHOT.value}
    assert record["upid"] == f"{UPID_FORMAT_ID}{title.content_id}" and record["mode"] == "CONTEXTUAL"
    other = policy_record(edge, stage, group, held_out, title.content_id)
    assert other is not None and other["contributed"][RECORD_ARM] == 0
