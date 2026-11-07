"""Content understanding (``modules.understanding``, M12) on a deployed stage: the test pattern's analysis, its item,
and, with the crawl module, its categories and keywords in the catalog context object and in a bidder's request.

Runs only when the stage has the Understanding stack. The test pattern of ``test_02_ingest`` starts the workflow on
its own ``Asset Ready``; on a stage that analysed an earlier run's pattern, the test starts the workflow again for
this run's asset (``"force": true``). It writes the pattern's catalog title for the crawl checks, sends one break of
the pattern through the guidance's edge as MediaTailor would, reads the bidder simulator's ``sim-bid-request`` line
of that auction, and removes the title, its object and the item at the end (so that the next run analyses its own
pattern). A run analyses about 70 seconds of video: well under a US dollar.

On a long-lived stage, whose ingest bucket takes only the sample titles, ``--understood <content_id>`` uses that
catalog title's READY encode instead: nothing is uploaded, the title is analysed only if it has no item yet, and its
item, catalog title and context object stay.
"""

import json
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Final

import pytest
from _stack import Module, Plane, Stage
from botocore.exceptions import ClientError
from harness import CONTENT_ID, wait_for
from harness_demand import DemandEdge

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import context_key
from fast_channels.core.crawl.taxonomy import default_genre_map
from fast_channels.core.demand import Parameter, simulator_function
from fast_channels.core.events import CatalogItemChanged, put_events_entry
from fast_channels.core.pds.buckets import bucket
from fast_channels.core.pds.names import CONTAINER
from fast_channels.core.timeline import UPID_FORMAT_ID
from fast_channels.core.understanding import Analysis, Status, Understanding, item_key
from fast_channels.functions.tools.catalog import catalog_item
from fast_channels.functions.tools.library_store import Asset, AssetStatus, asset_key

if TYPE_CHECKING:
    from harness import Channel
    from mypy_boto3_dynamodb.service_resource import Table

pytestmark = pytest.mark.e2e

ANALYSIS_TIMEOUT_S: Final = 30 * 60
"""Ingest to item: the proxy, the analyses and the model call took 6 minutes for the 70 s pattern (2026-11-07)."""
PUBLISH_TIMEOUT_S: Final = 90
EVID: Final = "1270410027"
TITLE: Final = ContentItem(
    content_id=CONTENT_ID,
    title="End-to-end test pattern",
    rating="TV-G",
    genres=["documentary"],
    duration_ms=70_000,
)
"""The pattern's catalog title, without a language: the understanding's fills it when the pattern has speech."""


@pytest.fixture(scope="module")
def understanding(stage: Stage) -> dict[str, str]:
    """The Understanding stack's outputs; skips the module when the stage has none."""
    if stage.status(Module.UNDERSTANDING) is None:
        pytest.skip(f"stage {stage.prefix} has no Understanding stack (modules.understanding is off)")
    return stage.outputs(Module.UNDERSTANDING)


@pytest.fixture(scope="module")
def catalog(stage: Stage) -> Table:
    return stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Plane.DATA, "CatalogTableName")
    )


@pytest.fixture(scope="module")
def title(request: pytest.FixtureRequest, library: Table, channel: Channel) -> tuple[str, Asset, bool]:
    """The title under test: its content id, its READY encode and whether the test owns it (the pattern, which it
    cleans up) or not (``--understood``, a catalog title the test leaves as it is)."""
    content_id = request.config.getoption("understood")
    if not content_id:
        return CONTENT_ID, request.getfixturevalue("ingested"), True
    item = library.get_item(Key=asset_key(content_id, channel.ladder_version)).get("Item")
    asset = Asset.from_item(item) if item else None
    if asset is None or asset.status is not AssetStatus.READY:
        pytest.fail(f"{content_id} has no READY encode on {channel.ladder_version}: seed it first (make seed)")
    return content_id, asset, False


@pytest.fixture(scope="module")
def understood(
    stage: Stage, understanding: dict[str, str], catalog: Table, title: tuple[str, Asset, bool], run_id: str
) -> Iterator[Understanding]:
    """The title's item for its asset, once the workflow wrote it; the pattern's is removed at the end."""
    content_id, ingested, owned = title
    current = catalog.get_item(Key=item_key(content_id)).get("Item")
    if (current is None and not owned) or (current and current.get("asset_id") != ingested.asset_id):
        # An encode that was READY before the module was on, or an earlier run's item: Prepare skips a title that
        # has one, so the asset is analysed by hand.
        sfn = stage.session.client("stepfunctions", region_name=stage.region)
        detail = {
            "asset_id": ingested.asset_id,
            "kind": "content",
            "ladder_version": ingested.ladder_version,
            "vod_source": None,
            "duration_ms": ingested.duration_ms or 70_000,
            "correlation_id": run_id,
        }
        sfn.start_execution(
            stateMachineArn=understanding["UnderstandingStateMachineArn"],
            name=f"e2e-{run_id}"[:80],
            input=json.dumps({"detail": detail, "force": True}),
        )

    def written() -> dict[str, Any] | None:
        item = catalog.get_item(Key=item_key(content_id)).get("Item")
        return dict(item) if item and item.get("asset_id") == ingested.asset_id else None

    started = time.monotonic()
    item = wait_for(written, timeout=ANALYSIS_TIMEOUT_S, interval=20, what=f"the understanding of {ingested.asset_id}")
    print(f"\nunderstood {time.monotonic() - started:.0f} s after the asset was READY:")
    print(json.dumps(item, default=str, indent=1))
    try:
        yield Understanding.model_validate(
            json.loads(json.dumps({k: v for k, v in item.items() if k not in {"pk", "sk"}}, default=_number))
        )
    finally:
        if owned:
            catalog.delete_item(Key=item_key(content_id))


def _number(value: Any) -> Any:
    """DynamoDB's Decimals as JSON numbers."""
    return int(value) if value == int(value) else float(value)


def test_the_title_is_understood(understood: Understanding) -> None:
    assert understood.status is Status.COMPLETE, understood
    assert set(understood.analyses) == set(Analysis)  # the stage runs every analysis
    assert understood.model is not None and understood.brand_safety is not None
    assert understood.synopsis and understood.keywords
    assert understood.shots is not None and understood.duration_ms > 30_000  # the pattern: 70 s


@pytest.fixture(scope="module")
def meta(catalog: Table, title: tuple[str, Asset, bool]) -> dict[str, Any]:
    """What the context object must keep of the catalog title: its genres' Content Taxonomy 3.1 ids and its
    language (the pattern's title names none)."""
    content_id, _, owned = title
    item: dict[str, Any] = TITLE.model_dump() if owned else dict(catalog.get_item(Key=catalog_key(content_id))["Item"])
    tags = [str(tag) for tag in item.get("genres") or []]
    return {
        "genres": [genre.id for genre in default_genre_map().resolve(tags).genres],
        "language": item.get("language"),
    }


@pytest.fixture(scope="module")
def published(
    stage: Stage, catalog: Table, title: tuple[str, Asset, bool], understood: Understanding, run_id: str
) -> Iterator[dict[str, Any]]:
    """The title's catalog context object, once the publisher read its catalog title and its understanding."""
    if stage.status(Module.CRAWL) is None:
        pytest.skip(f"stage {stage.prefix} has no Crawl stack (modules.crawl is off)")
    content_id, _, owned = title
    s3 = stage.session.client("s3", region_name=stage.region)
    objects = stage.output(Module.CRAWL, "CatalogContextBucketName")
    if owned:
        catalog.put_item(Item=catalog_item(TITLE))
    entry = put_events_entry(
        CatalogItemChanged(content_id=content_id, correlation_id=run_id),
        prefix=stage.prefix,
        event_bus=stage.output(Plane.MEDIA, "EventBusName"),
    )
    events = stage.session.client("events", region_name=stage.region)
    assert events.put_events(Entries=[entry])["FailedEntryCount"] == 0  # type: ignore[list-item]

    def enriched() -> dict[str, Any] | None:
        try:
            body = s3.get_object(Bucket=objects, Key=context_key(content_id))["Body"].read()
        except ClientError:
            return None
        document: dict[str, Any] = json.loads(body)
        return document if set(understood.keywords) <= set(document["kwarray"]) else None

    try:
        yield wait_for(enriched, timeout=PUBLISH_TIMEOUT_S, interval=3, what="the enriched context object")
    finally:
        if owned:
            catalog.delete_item(Key=catalog_key(content_id))
            s3.delete_object(Bucket=objects, Key=context_key(content_id))


def catalog_key(content_id: str) -> dict[str, str]:
    return {"pk": f"CONTENT#{content_id}", "sk": "META"}


def test_the_catalog_context_carries_the_understanding(
    published: dict[str, Any], understood: Understanding, meta: dict[str, Any]
) -> None:
    print("context object:", json.dumps(published))
    assert published["genres"] == meta["genres"]  # the catalog's own genres stay (the pattern's: documentary, 332)
    assert published["cat"][: len(understood.cat[:3])] == list(understood.cat[:3])  # the understanding leads cat
    assert set(understood.keywords) <= set(published["kwarray"])
    assert published["language"] == (meta["language"] or understood.language or "")  # the catalog's first


def test_a_bidders_request_carries_the_understanding(
    stage: Stage, title: tuple[str, Asset, bool], published: dict[str, Any], understood: Understanding, run_id: str
) -> None:
    """One contextual break of the pattern through the guidance's edge: the PDS patches ``app.content`` from its copy
    of the catalog, and the bidder simulator logs the request's categories and keywords (overlay O5)."""
    if stage.status(Module.PDS) is None:
        pytest.skip(f"stage {stage.prefix} has no Pds stack (deploy-demand)")
    edge = DemandEdge(stage.prefix, stage.session)
    percent = int(sidecar_environment(edge, stage)["CRAWL_HOLDOUT_PERCENT"])
    sid = enriched_session(run_id, percent)
    logs = stage.session.client("logs", region_name=stage.region)
    group = f"/aws/lambda/{simulator_function(stage.prefix)}"

    def requested() -> dict[str, Any] | None:
        since = int(time.time() * 1000) - 1000
        response = edge.auction(60, sid=sid, evid=EVID, upid=f"{UPID_FORMAT_ID}{title[0]}", probe=False)
        assert response.status_code in {200, 204}, (response.status_code, response.text[:300])
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            found = logs.filter_log_events(logGroupName=group, startTime=since, filterPattern='"sim-bid-request"')
            for line in (simulator_line(event["message"]) for event in found["events"]):
                if line.get("content_kwarray") and set(understood.keywords) <= set(line["content_kwarray"]):
                    return line
            time.sleep(3)
        return None

    line = wait_for(requested, timeout=120, interval=10, what="a bid request with the understanding")
    print("bid request:", json.dumps(line))
    assert line["content_cat"][: len(understood.cat[:3])] == list(understood.cat[:3])


def enriched_session(run_id: str, percent: int) -> str:
    """A session id of the run whose break :data:`EVID` is outside the ``crawl-v1`` holdout (it gets the patch)."""
    for n in range(1000):
        sid = f"{run_id}-u{n}"[-64:]
        if bucket("crawl-v1", f"{sid}.{EVID}") >= percent:
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


def simulator_line(message: str) -> dict[str, Any]:
    """The ``sim-bid-request`` object of a log event (Lambda's JSON log format wraps the logged string)."""
    record = json.loads(message)
    logged = record.get("message", record) if isinstance(record, dict) else record
    line = json.loads(logged) if isinstance(logged, str) else logged
    return dict(line) if isinstance(line, dict) else {}
