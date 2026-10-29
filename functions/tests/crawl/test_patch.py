"""The crawl module's ``/policy`` patch contributor: the PDS's copy of the catalog, the ``crawl-v1`` arm, the header
fallback, and the policy function with the module on."""

import base64
import io
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from fast_channels.core.catalog import ContentItem
from fast_channels.core.crawl.catalog_context import CONTEXT_PREFIX, CatalogContext, build_context, context_key
from fast_channels.core.crawl.names import Source
from fast_channels.core.crawl.taxonomy import default_genre_map, load_taxonomy
from fast_channels.core.pds.buckets import bucket
from fast_channels.core.pds.contributors import Contribution
from fast_channels.core.pds.models import Mode, PolicyRequest
from fast_channels.core.pds.snapshot import SnapshotBase, build, demo_demand
from fast_channels.functions.crawl import patch
from fast_channels.functions.crawl.patch import CatalogContexts, CrawlContributor, arm
from fast_channels.functions.pds import policy
from fast_channels.functions.pds.store import RETRY_SECONDS
from fast_channels.functions.settings import settings

REPO = Path(__file__).resolve().parents[3]
BUCKET = "fastm-media-catalogcontextsbucket"
PUBLISHER = "example.com"
SAMPLES = {
    title["content_id"]: ContentItem.model_validate(title)
    for title in json.loads((REPO / "assets/sample-catalog.json").read_text())["titles"]
}
CONTEXTS = {
    content: build_context(
        item, genre_map=default_genre_map(), publisher_domain=PUBLISHER, updated=datetime(2026, 10, 28, tzinfo=UTC)
    )
    for content, item in SAMPLES.items()
}


def inline(target: Callable[[], None]) -> None:
    target()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def body(text: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(text), len(text))


def sid_in(arm_wanted: int, percent: int = 20, evid: str = "820461826") -> str:
    """A session id whose break ``evid`` falls in ``arm_wanted``."""
    for n in range(1000):
        sid = f"6f1c2a9e-4b7d-4e2a-9c1e-{n:012d}"
        if (bucket("crawl-v1", f"{sid}.{evid}") >= percent) == bool(arm_wanted):
            return sid
    raise AssertionError("no such session")


ENRICHED, HELD_OUT = sid_in(1), sid_in(0)


def request(**fields: Any) -> PolicyRequest:
    base: dict[str, Any] = {
        "v": 1,
        "sid": ENRICHED,
        "pc": "fastm-demo-p-b",
        "evid": "820461826",
        "upid": "tears-of-steel",
        "dur_ms": 60060,
        "ctx": "",
    }
    return PolicyRequest.model_validate(base | fields)


def contributor(contexts: dict[str, CatalogContext] | None = None, *, native: bool = False) -> CrawlContributor:
    copy = CONTEXTS if contexts is None else contexts
    return CrawlContributor(
        contexts=lambda: copy,
        holdout_percent=20,
        native=native,
        publisher_domain=PUBLISHER,
        genre_names=load_taxonomy(),
    )


# --- the arm --------------------------------------------------------------------------------------------------------


def test_the_arm_is_the_crawl_v1_bucket_of_the_session_break() -> None:
    assert (arm(ENRICHED, "820461826", 20), arm(HELD_OUT, "820461826", 20)) == (1, 0)
    assert arm(ENRICHED, "820461826", None) == 0, "module off: every break is unenriched"
    assert arm(ENRICHED, "820461826", 100) == 0 and arm(HELD_OUT, "820461826", 0) == 1


def test_the_holdout_is_its_share_of_session_breaks() -> None:
    breaks = [(f"s{n}", str(evid)) for n in range(500) for evid in range(10)]
    share = sum(1 - arm(sid, evid, 20) for sid, evid in breaks) / len(breaks)
    assert 0.19 <= share <= 0.21


# --- the contributor ------------------------------------------------------------------------------------------------


def test_the_enriched_arm_gets_the_titles_content_from_the_copy() -> None:
    contribution = contributor().contribute(request(), Mode.CONTEXTUAL)
    assert contribution.record == {"enr": 1, "ctx": "snapshot"}
    content = contribution.app_content
    assert (content["id"], content["title"], content["channel"]) == (
        "tears-of-steel",
        "Tears of Steel",
        {"id": "fastm-demo", "domain": PUBLISHER},
    )
    assert content["ext"] == {
        "realtime": 0,
        "firstbroadcast": 0,
        "genres": ["652", "325"],
        "gtax": 9,
        "data": [{"name": PUBLISHER, "cids": ["tears-of-steel"]}],
    }


def test_on_4_4_the_fields_are_native() -> None:
    content = contributor(native=True).contribute(request(), Mode.CONTEXTUAL).app_content
    assert "ext" not in content and content["genres"] == ["652", "325"] and content["gtax"] == 9


def test_the_holdout_gets_nothing_but_its_record() -> None:
    contribution = contributor().contribute(request(sid=HELD_OUT, ctx=CONTEXTS["tears-of-steel"].hdr), Mode.FULL)
    assert contribution == Contribution(record={"enr": 0, "ctx": "snapshot"})


@pytest.mark.parametrize("upid", ["FST3tears-of-steel", "tears-of-steel"])
def test_the_upid_with_or_without_its_format_identifier(upid: str) -> None:
    assert contributor().contribute(request(upid=upid), Mode.CONTEXTUAL).app_content["id"] == "tears-of-steel"


def test_the_header_fills_in_while_the_copy_lacks_the_title() -> None:
    hdr = CONTEXTS["tears-of-steel"].hdr
    contribution = contributor({}).contribute(request(ctx=hdr), Mode.CONTEXTUAL)
    assert contribution.record == {"enr": 1, "ctx": "header"}
    content = contribution.app_content
    assert (content["id"], content["genre"], content["len"]) == ("tears-of-steel", "Science Fiction", 734)
    assert "title" not in content, "the header has no title"


@pytest.mark.parametrize(
    ("fields", "contexts"),
    [
        ({"upid": ""}, None),
        ({"upid": "FST3not a content id"}, None),
        ({"upid": "no-such-title"}, None),
        ({"ctx": CONTEXTS["sintel"].hdr}, {}),  # a header naming another title
        ({"ctx": "v9;id=tears-of-steel"}, {}),
    ],
)
def test_no_metadata_no_content(fields: dict[str, Any], contexts: dict[str, CatalogContext] | None) -> None:
    contribution = contributor(contexts).contribute(request(**fields), Mode.CONTEXTUAL)
    assert contribution == Contribution(record={"enr": 1, "ctx": Source.NONE.value})


def test_pseudonymous_and_safe() -> None:
    pseudonymous = contributor().contribute(request(), Mode.PSEUDONYMOUS).app_content
    assert "title" not in pseudonymous and "id" not in pseudonymous and pseudonymous["genre"] == "Science Fiction"
    safe = contributor().contribute(request(), Mode.SAFE)
    assert safe == Contribution(record={"enr": 1, "ctx": "snapshot"})


# --- the copy of the catalog ----------------------------------------------------------------------------------------


@pytest.fixture
def s3() -> Iterator[tuple[Stubber, Any]]:
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="t", aws_secret_access_key="t")
    with Stubber(client) as stubber:
        yield stubber, client
        stubber.assert_no_pending_responses()


def listing(stubber: Stubber, objects: dict[str, str]) -> None:
    contents = [{"Key": context_key(content), "ETag": etag} for content, etag in objects.items()]
    stubber.add_response(
        "list_objects_v2", {"Contents": contents, "IsTruncated": False}, {"Bucket": BUCKET, "Prefix": CONTEXT_PREFIX}
    )


def reading(stubber: Stubber, content: str, text: bytes | None = None) -> None:
    payload = text if text is not None else CONTEXTS[content].model_dump_json().encode()
    stubber.add_response("get_object", {"Body": body(payload)}, {"Bucket": BUCKET, "Key": context_key(content)})


def test_loads_the_bucket_then_reads_only_what_changed(s3: tuple[Stubber, Any]) -> None:
    stubber, client = s3
    clock = Clock()
    copy = CatalogContexts(BUCKET, client=client, clock=clock, spawn=inline)
    listing(stubber, {"sintel": '"a"', "tears-of-steel": '"b"'})
    reading(stubber, "sintel")
    reading(stubber, "tears-of-steel")
    assert copy.load() == {"sintel": CONTEXTS["sintel"], "tears-of-steel": CONTEXTS["tears-of-steel"]}

    clock.now += patch.REFRESH_SECONDS
    listing(stubber, {"sintel": '"a"', "tears-of-steel": '"b"'})
    assert set(copy.get()) == {"sintel", "tears-of-steel"}, "unchanged: one list, no GET"

    clock.now += patch.REFRESH_SECONDS
    changed = CONTEXTS["tears-of-steel"].model_copy(update={"contentrating": "TV-MA"})
    listing(stubber, {"tears-of-steel": '"c"', "elephants-dream": '"d"'})
    reading(stubber, "tears-of-steel", changed.model_dump_json().encode())
    reading(stubber, "elephants-dream")
    assert copy.get() == {"tears-of-steel": changed, "elephants-dream": CONTEXTS["elephants-dream"]}


def test_an_invalid_object_is_skipped(s3: tuple[Stubber, Any], caplog: pytest.LogCaptureFixture) -> None:
    stubber, client = s3
    copy = CatalogContexts(BUCKET, client=client, spawn=inline)
    listing(stubber, {"sintel": '"a"', "tears-of-steel": '"b"', "elephants-dream": '"c"'})
    reading(stubber, "sintel", b"not json")
    reading(stubber, "tears-of-steel")
    reading(stubber, "elephants-dream", CONTEXTS["sintel"].model_dump_json().encode())  # under another title's key
    assert copy.load() == {"tears-of-steel": CONTEXTS["tears-of-steel"]}
    [warning] = [r for r in caplog.records if r.getMessage() == "catalog contexts skipped"]
    assert vars(warning)["invalid"] == ["sintel", "elephants-dream"]


def test_a_failed_refresh_keeps_the_copy_and_retries_sooner(s3: tuple[Stubber, Any]) -> None:
    stubber, client = s3
    clock = Clock()
    copy = CatalogContexts(BUCKET, client=client, clock=clock, spawn=inline)
    listing(stubber, {"sintel": '"a"'})
    reading(stubber, "sintel")
    copy.load()

    clock.now += patch.REFRESH_SECONDS
    stubber.add_client_error("list_objects_v2", service_error_code="AccessDenied", http_status_code=403)
    assert copy.get() == {"sintel": CONTEXTS["sintel"]}
    clock.now += RETRY_SECONDS
    listing(stubber, {"sintel": '"a"'})
    assert copy.get() == {"sintel": CONTEXTS["sintel"]}


# --- configuration and the policy function --------------------------------------------------------------------------


@pytest.fixture
def crawl_on(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "CRAWL_HOLDOUT_PERCENT": "20",
        "CATALOG_CONTEXT_BUCKET": BUCKET,
        "PBS_VERSION": "3.43.0",
        "PUBLISHER_DOMAIN": PUBLISHER,
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    patch.contributor.cache_clear()
    patch.catalog_contexts.cache_clear()
    yield
    settings.cache_clear()
    patch.contributor.cache_clear()
    patch.catalog_contexts.cache_clear()


@pytest.fixture
def crawl_off() -> Iterator[None]:
    settings.cache_clear()
    patch.contributor.cache_clear()
    yield
    settings.cache_clear()
    patch.contributor.cache_clear()


@pytest.mark.usefixtures("crawl_off")
def test_off_without_the_holdout_setting() -> None:
    assert patch.contributor() is None
    assert policy.contributors() == ()


@pytest.mark.usefixtures("crawl_on")
@pytest.mark.parametrize(("version", "native"), [("3.43.0", False), ("4.4.0", True)])
def test_on_with_the_stages_settings(monkeypatch: pytest.MonkeyPatch, version: str, native: bool) -> None:
    monkeypatch.setenv("PBS_VERSION", version)
    settings.cache_clear()
    built = patch.contributor()
    assert built is not None
    assert (built.holdout_percent, built.native, built.publisher_domain) == (20, native, PUBLISHER)
    assert patch.catalog_contexts()._bucket == BUCKET
    assert policy.contributors() == (built,)


@pytest.mark.usefixtures("crawl_on")
@pytest.mark.parametrize("missing", ["CATALOG_CONTEXT_BUCKET", "PBS_VERSION"])
def test_on_without_its_bucket_or_release_fails_loudly(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.delenv(missing)
    settings.cache_clear()
    with pytest.raises(RuntimeError, match=missing):
        patch.contributor()


class _Snapshot:
    def get(self) -> Any:
        return build(SnapshotBase(demand=demo_demand("fastm")), now=datetime(2026, 10, 29, tzinfo=UTC))


def test_the_policy_answer_and_record_with_the_module_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(policy, "store", _Snapshot)
    monkeypatch.setattr(policy, "contributor", contributor)
    event = {
        "requestContext": {"elb": {"targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/pds/1"}},
        "httpMethod": "POST",
        "path": "/policy",
        "queryStringParameters": {},
        "headers": {"content-type": "application/json"},
        "body": base64.b64encode(request().model_dump_json().encode()).decode(),
        "isBase64Encoded": True,
    }
    context = type(
        "Context",
        (),
        {
            "function_name": "fastm-pds-policy",
            "function_version": "1",
            "memory_limit_in_mb": 1024,
            "invoked_function_arn": "arn:aws:lambda:us-east-1:123456789012:function:fastm-pds-policy:live",
            "aws_request_id": "r",
            "log_group_name": "g",
            "log_stream_name": "s",
            "get_remaining_time_in_millis": lambda self: 3000,
        },
    )()

    response = policy.handler(event, context)

    answer = json.loads(response["body"])
    assert answer["app_content"]["title"] == "Tears of Steel"
    [record] = [vars(r) for r in caplog.records if getattr(r, "record", None) == "pds.policy"]
    assert (record["contributors"], record["skipped"], record["contributed"]) == (
        ["crawl"],
        [],
        {"enr": 1, "ctx": "snapshot"},
    )
