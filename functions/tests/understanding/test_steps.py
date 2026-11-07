"""The understanding workflow's steps: prepare (skip or plan), collect (results to signals) and describe (the model
call, the item and the events), with botocore Stubbers on every client."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.stub import ANY, Stubber

from fast_channels.core.understanding.record import TASK
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.understanding import steps

LIBRARY, CATALOG, MEDIA, BUCKET, BUS = "library-test", "catalog-test", "media-test", "understanding-test", "bus"
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
ENTRY = {
    "model": "us.amazon.nova-2-lite-v1:0",
    "route": "bedrock",
    "effort": "low",
    "thinking": "disabled",
    "max_tokens": 4000,
    "data_class": "operational",
    "daily_cap_usd": 5,
    "eval": "amazon.nova-2-lite-v1-0-disabled-4k",
    "profile_arn": PROFILE,
}
READY = {
    "asset_id": "sintel-20261107",
    "kind": "content",
    "ladder_version": "standard@843ed1d05a",
    "vod_source": "sintel-20261107",
    "duration_ms": 888_000,
    "correlation_id": "seed-sintel",
}
MULTIVARIANT = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\nhls_1080p.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nhls_360p.m3u8\n"
)
ASSET_PREFIX = "assets/sintel-20261107/standard_843ed1d05a/"
_serialize = TypeSerializer().serialize


@dataclass
class Context:
    function_name: str = "understanding"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:understanding"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/understanding"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 300_000


CONTEXT = cast("Any", Context())


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("FAST_PREFIX", "fastm")
    monkeypatch.setenv("LIBRARY_TABLE", LIBRARY)
    monkeypatch.setenv("CATALOG_TABLE", CATALOG)
    monkeypatch.setenv("MEDIA_BUCKET", MEDIA)
    monkeypatch.setenv("UNDERSTANDING_BUCKET", BUCKET)
    monkeypatch.setenv("UNDERSTANDING_ANALYSES", "transcript,shots,technical_cues,labels,moderation")
    monkeypatch.setenv("UNDERSTANDING_TASK", TASK)
    monkeypatch.setenv("MODELS_COUNTER_TABLE", "fastm-models")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:fastm-alerts")
    monkeypatch.setenv("EVENT_BUS", BUS)
    settings.cache_clear()
    steps.registry_entry.cache_clear()
    yield
    settings.cache_clear()
    steps.registry_entry.cache_clear()


def stubber(client: Any) -> Iterator[Stubber]:
    with Stubber(client) as stub:
        yield stub
        stub.assert_no_pending_responses()


@pytest.fixture
def library() -> Iterator[Stubber]:
    yield from stubber(aws.table(LIBRARY).meta.client)


@pytest.fixture
def catalog() -> Iterator[Stubber]:
    yield from stubber(aws.table(CATALOG).meta.client)


@pytest.fixture
def s3() -> Iterator[Stubber]:
    yield from stubber(aws.client("s3"))


@pytest.fixture
def rekognition() -> Iterator[Stubber]:
    yield from stubber(aws.client("rekognition"))


@pytest.fixture
def ssm() -> Iterator[Stubber]:
    yield from stubber(aws.client("ssm"))


@pytest.fixture
def counters() -> Iterator[Stubber]:
    yield from stubber(aws.client("dynamodb"))


@pytest.fixture
def events() -> Iterator[Stubber]:
    yield from stubber(aws.client("events"))


@pytest.fixture
def bedrock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    monkeypatch.setattr(steps, "_bedrock", lambda: client)
    yield from stubber(client)


def dynamo(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


# --- prepare -------------------------------------------------------------------------------------------------------


def expect_asset(library: Stubber, content_id: str | None) -> None:
    item = {"pk": "ASSET#sintel-20261107", "sk": "LADDER#standard@843ed1d05a", "asset_id": "sintel-20261107"}
    if content_id:
        item["content_id"] = content_id
    library.add_response(
        "get_item",
        {"Item": dynamo(item)},
        {"TableName": LIBRARY, "Key": {"pk": "ASSET#sintel-20261107", "sk": "LADDER#standard@843ed1d05a"}},
    )


def expect_understanding(catalog: Stubber, item: dict[str, Any] | None) -> None:
    catalog.add_response(
        "get_item",
        {"Item": dynamo(item)} if item else {},
        {"TableName": CATALOG, "Key": {"pk": "CONTENT#sintel", "sk": "UNDERSTANDING"}},
    )


def test_a_title_is_planned_from_its_smallest_360p_variant(library: Stubber, catalog: Stubber, s3: Stubber) -> None:
    expect_asset(library, "sintel")
    expect_understanding(catalog, None)
    s3.add_response(
        "get_object",
        {"Body": cast("Any", _Body(MULTIVARIANT.encode()))},
        {"Bucket": MEDIA, "Key": f"{ASSET_PREFIX}main.m3u8"},
    )

    plan = steps.prepare({"detail": READY, "execution": "exec-1"}, CONTEXT)

    assert plan["skip"] is False
    assert (plan["content_id"], plan["asset_id"], plan["duration_ms"]) == ("sintel", "sintel-20261107", 888_000)
    assert plan["proxy_key"] == "proxies/sintel/sintel-20261107/proxy.mp4"
    assert plan["transcript_key"] == "results/sintel/sintel-20261107/transcript.json"
    assert plan["transcription_job"] == "fastm-exec-1"
    job_input = plan["job"]["Settings"]["Inputs"][0]["FileInput"]
    assert job_input == f"s3://{MEDIA}/{ASSET_PREFIX}hls_360p.m3u8"
    destination = plan["job"]["Settings"]["OutputGroups"][0]["OutputGroupSettings"]["FileGroupSettings"]
    assert destination["Destination"] == f"s3://{BUCKET}/proxies/sintel/sintel-20261107/proxy"
    assert plan["job"]["UserMetadata"]["content_id"] == "sintel"


@pytest.mark.parametrize(
    ("detail", "content_id", "existing", "reason"),
    [
        ({**READY, "kind": "ad"}, None, None, "a ad asset is not a title"),
        (READY, None, None, "names no catalog title"),
        (READY, "sintel", {"pk": "CONTENT#sintel", "sk": "UNDERSTANDING", "asset_id": "old"}, "analysed already"),
    ],
)
def test_nothing_to_analyse(
    library: Stubber,
    catalog: Stubber,
    detail: dict[str, Any],
    content_id: str | None,
    existing: dict[str, Any] | None,
    reason: str,
) -> None:
    if detail["kind"] == "content":
        expect_asset(library, content_id)
        if content_id:
            expect_understanding(catalog, existing)

    plan = steps.prepare({"detail": detail}, CONTEXT)

    assert plan["skip"] is True and reason in plan["reason"]


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


# --- collect -------------------------------------------------------------------------------------------------------


def prepared() -> dict[str, Any]:
    return steps.Prepared(
        skip=False,
        content_id="sintel",
        asset_id="sintel-20261107",
        duration_ms=888_000,
        correlation_id="exec-1",
        bucket=BUCKET,
        proxy_key="proxies/sintel/sintel-20261107/proxy.mp4",
        results="results/sintel/sintel-20261107/",
        transcript_key="results/sintel/sintel-20261107/transcript.json",
        transcription_job="fastm-exec-1",
        token="exec-1",
    ).model_dump(mode="json")


TRANSCRIPT = {
    "results": {
        "language_code": "en-US",
        "transcripts": [{"transcript": "Where is my dragon?"}],
        "items": [{"type": "pronunciation", "start_time": "1.0", "end_time": "89.8"}],
    }
}


def test_collect_reduces_every_finished_analysis(s3: Stubber, rekognition: Stubber) -> None:
    results = "results/sintel/sintel-20261107/"
    s3.add_response(
        "get_object",
        {"Body": cast("Any", _Body(json.dumps(TRANSCRIPT).encode()))},
        {"Bucket": BUCKET, "Key": f"{results}transcript.json"},
    )
    rekognition.add_response(
        "get_segment_detection",
        {
            "JobStatus": "SUCCEEDED",
            "Segments": [{"Type": "SHOT", "DurationMillis": 5000}, {"Type": "SHOT", "DurationMillis": 7000}],
            "NextToken": "page-2",
        },
        {"JobId": "seg-1", "MaxResults": 1000},
    )
    rekognition.add_response(
        "get_segment_detection",
        {
            "JobStatus": "SUCCEEDED",
            "Segments": [
                {
                    "Type": "TECHNICAL_CUE",
                    "TechnicalCueSegment": {"Type": "EndCredits", "Confidence": 99.0},
                    "StartTimestampMillis": 800_000,
                }
            ],
        },
        {"JobId": "seg-1", "MaxResults": 1000, "NextToken": "page-2"},
    )
    s3.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": f"{results}segments.json", "Body": ANY, "ContentType": ANY}
    )
    rekognition.add_response(
        "get_label_detection",
        {
            "JobStatus": "SUCCEEDED",
            "Labels": [{"Label": {"Name": "Dragon", "Confidence": 97.0}, "DurationMillis": 9000}],
        },
        {"JobId": "lab-1", "MaxResults": 1000, "AggregateBy": "SEGMENTS"},
    )
    s3.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": f"{results}labels.json", "Body": ANY, "ContentType": ANY}
    )
    s3.add_response(
        "put_object", {}, {"Bucket": BUCKET, "Key": f"{results}signals.json", "Body": ANY, "ContentType": ANY}
    )
    analyses = [
        [{"analysis": "transcript", "job_id": "fastm-exec-1"}],
        [{"analysis": "shots", "job_id": "seg-1"}, {"analysis": "technical_cues", "job_id": "seg-1"}],
        [{"analysis": "labels", "job_id": "lab-1"}],
        [{"analysis": "moderation", "error": '{"Error":"Rekognition.LimitExceededException"}'}],
    ]

    collected = steps.collect({"prepared": prepared(), "analyses": analyses}, CONTEXT)

    signals = collected["signals"]
    assert collected["failed"] == ["moderation"]
    assert signals["analyses"] == ["transcript", "shots", "technical_cues", "labels"]
    assert (signals["language"], signals["speech_ratio"]) == ("en", 0.1)
    assert (signals["shots"], signals["mean_shot_s"], signals["credits_at_s"]) == (2, 6.0, 800.0)
    assert signals["labels"] == [{"name": "Dragon", "seconds": 9.0}]
    assert signals["moderation"] == {}


# --- describe ------------------------------------------------------------------------------------------------------


def converse_response(tool_input: dict[str, Any]) -> dict[str, Any]:
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": "record_understanding", "input": tool_input}}],
            }
        },
        "stopReason": "tool_use",
        "usage": {"inputTokens": 250_000, "outputTokens": 300, "totalTokens": 250_300},
        "metrics": {"latencyMs": 9000},
    }


def describe_event(**signals: Any) -> dict[str, Any]:
    return {
        "prepared": prepared(),
        "signals": {
            "duration_ms": 888_000,
            "analyses": ["transcript", "shots", "technical_cues", "labels", "moderation"],
            "language": "en",
            "speech_ratio": 0.4,
            "transcript": "Where is my dragon?",
            "shots": 120,
            "moderation": {"Violence": 96.0},
            "moderation_labels": {
                "Violence": {"category": "Violence", "frames": 14, "sustained": 12, "confidence": 96.0},
                "Blood & Gore": {"category": "Violence", "frames": 14, "sustained": 12, "confidence": 96.0},
            },
            **signals,
        },
        "failed": [],
    }


def expect_entry(ssm: Stubber) -> None:
    ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": f"/fastm/models/tasks/{TASK}", "Value": json.dumps(ENTRY)}},
        {"Name": f"/fastm/models/tasks/{TASK}"},
    )


def expect_events(events: Stubber) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    events.add_response("put_events", {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}, {"EventId": "2"}]}, None)
    return sent


def written(catalog: Stubber) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def keep(params: dict[str, Any], **_: Any) -> None:
        captured.update(params)

    catalog.client.meta.events.register("provide-client-params.dynamodb.PutItem", keep)
    catalog.add_response("put_item", {}, None)
    return captured


def test_the_model_describes_the_title_and_the_item_is_complete(
    ssm: Stubber, counters: Stubber, bedrock: Stubber, catalog: Stubber, events: Stubber
) -> None:
    expect_entry(ssm)
    counters.add_response("update_item", {}, None)  # admit
    bedrock.add_response(
        "converse",
        converse_response(
            {
                "genres": ["Animation", "Fantasy", "made-up"],
                "keywords": ["Dragon", "quest"],
                "moods": ["epic", "sad"],
                "brand_safety": "low",
                "synopsis": "A young woman searches for the dragon she once raised.",
            }
        ),
        None,
    )
    counters.add_response("update_item", {}, None)  # add the cost
    item = written(catalog)
    expect_events(events)

    result = steps.describe(describe_event(), CONTEXT)

    assert result["status"] == "complete"
    stored = item["Item"]
    assert (stored["pk"], stored["sk"]) == ("CONTENT#sintel", "UNDERSTANDING")
    assert stored["cat"] == ["641", "335"]
    assert stored["keywords"] == ["dragon", "quest"]
    assert stored["moods"] == ["epic", "sad"]
    assert stored["brand_safety"] == "high"  # moderation saw sustained blood and gore, the model rated it low
    assert stored["moderation"] == ["Violence"]
    assert stored["model"] == "amazon.nova-2-lite-v1:0"
    assert stored["status"] == "complete" and stored["language"] == "en"


def test_without_the_model_the_item_is_partial(
    ssm: Stubber, counters: Stubber, bedrock: Stubber, catalog: Stubber, events: Stubber
) -> None:
    expect_entry(ssm)
    counters.add_response("update_item", {}, None)
    bedrock.add_client_error("converse", service_error_code="ValidationException", http_status_code=400)
    item = written(catalog)
    expect_events(events)

    result = steps.describe(describe_event(), CONTEXT)

    stored = item["Item"]
    assert result["status"] == "partial"
    assert (stored["status"], stored["cat"], stored["synopsis"], stored["model"]) == ("partial", [], "", None)
    assert stored["brand_safety"] == "high"  # the moderation labels still give the tier


def test_a_reached_cap_skips_the_model(ssm: Stubber, counters: Stubber, catalog: Stubber, events: Stubber) -> None:
    expect_entry(ssm)
    counters.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
    counters.add_response("update_item", {}, None)  # the notice, once a day
    aws_sns = Stubber(aws.client("sns"))
    aws_sns.add_response("publish", {"MessageId": "m"}, None)
    item = written(catalog)
    expect_events(events)

    with aws_sns:
        result = steps.describe(describe_event(), CONTEXT)

    assert result["status"] == "partial" and item["Item"]["model"] is None
