"""The creatives module's readiness steps (M5): registry transitions, the quarantine copy, moderation, conformance and
the READY creatives published to the own book."""

import copy
import functools
import io
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from botocore.exceptions import ClientError

from fast_channels.core.creatives.names import READY_KEY, crid_for, source_key
from fast_channels.core.creatives.registry import ReadyCreatives, State, TransitionError
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import AUDIO_CODEC
from fast_channels.core.naming import digest
from fast_channels.functions import aws
from fast_channels.functions.creatives import readiness
from fast_channels.functions.settings import settings

LADDER = Ladder.builtin("standard")
QUARANTINE, MEDIA, CONFIG, PUBLISHER = "fastm-creatives-q", "fastm-media", "fastm-pds-config", "fastm-publisher"
ADOMAIN = "acme-outdoor.example"
CRID = crid_for(ADOMAIN, "e2e-spot")
UPLOAD = f"s3://{QUARANTINE}/{source_key(CRID)}"
CDN = "https://d111111abcdef8.cloudfront.net"
CREATIVE_ID = f"{CRID}-standard_843ed1d05a-20d4b4"
AD_PREFIX = f"ads/standard_843ed1d05a/{CREATIVE_ID}/"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


# --- fakes -----------------------------------------------------------------------------------------------------------


def _failed(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, operation)


@dataclass
class Table:
    """The registry table: GetItem, the conditional UpdateItem of ``update_request`` and the state index."""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get_item(self, Key: Mapping[str, str], ConsistentRead: bool = False) -> dict[str, Any]:  # noqa: N803
        found = self.items.get(Key["crid"])
        return {"Item": copy.deepcopy(found)} if found is not None else {}

    def update_item(self, **request: Any) -> None:
        crid = request["Key"]["crid"]
        current = self.items.get(crid)
        expression = request["ConditionExpression"].get_expression()
        name = expression["values"][0].name
        match expression["operator"]:
            case "attribute_not_exists":
                holds = current is None or name not in current
            case "=":
                holds = current is not None and current.get(name) == expression["values"][1]
            case other:
                raise AssertionError(other)
        if not holds:
            raise _failed("UpdateItem")
        names, values = request["ExpressionAttributeNames"], request.get("ExpressionAttributeValues", {})
        updated = dict(current or {"crid": crid})
        for placeholder, attribute in names.items():
            updated[attribute] = copy.deepcopy(values[f":{placeholder[1:]}"])
        self.items[crid] = updated

    def query(self, **request: Any) -> dict[str, Any]:
        assert request["IndexName"] == "by-state"
        state = request["KeyConditionExpression"].get_expression()["values"][1]
        return {"Items": [copy.deepcopy(i) for i in self.items.values() if i.get("state") == state]}


class NoSuchKeyError(ClientError):
    def __init__(self) -> None:
        super().__init__({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")


@dataclass
class S3:
    objects: dict[tuple[str, str], bytes] = field(default_factory=dict)
    exceptions: Any = field(default_factory=lambda: type("Exceptions", (), {"NoSuchKey": NoSuchKeyError}))

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise NoSuchKeyError
        return {"Body": io.BytesIO(self.objects[Bucket, Key])}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:  # noqa: N803
        self.objects[Bucket, Key] = Body

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        return {"ContentLength": len(self.objects[Bucket, Key])}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: Any = None) -> None:  # noqa: N803
        self.objects[Bucket, Key] = Path(Filename).read_bytes()


@dataclass
class Rekognition:
    pages: list[dict[str, Any]] = field(default_factory=list)
    started: list[dict[str, Any]] = field(default_factory=list)

    def start_content_moderation(self, **request: Any) -> dict[str, str]:
        self.started.append(request)
        return {"JobId": "job-1"}

    def get_content_moderation(self, **request: Any) -> dict[str, Any]:
        return self.pages.pop(0)


@dataclass
class Lambda:
    invoked: list[dict[str, Any]] = field(default_factory=list)

    def invoke(self, **request: Any) -> None:
        self.invoked.append(request)


@dataclass
class Fakes:
    table: Table = field(default_factory=Table)
    s3: S3 = field(default_factory=S3)
    rekognition: Rekognition = field(default_factory=Rekognition)
    lambda_: Lambda = field(default_factory=Lambda)


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    fake = Fakes()
    for name, value in {
        "CREATIVES_TABLE": "fastm-creatives",
        "CREATIVES_QUARANTINE_BUCKET": QUARANTINE,
        "CREATIVES_LADDER": LADDER.model_dump_json(),
        "CREATIVES_MODERATION": "true",
        "CREATIVES_MIN_CONFIDENCE": "80",
        "MEDIA_BUCKET": MEDIA,
        "PDS_CONFIG_BUCKET": CONFIG,
        "PDS_CREATIVES_KEY": READY_KEY,
        "PDS_PUBLISHER_FUNCTION": PUBLISHER,
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    clients = {"s3": fake.s3, "rekognition": fake.rekognition, "lambda": fake.lambda_}
    monkeypatch.setattr(aws, "client", clients.__getitem__)
    monkeypatch.setattr(aws, "table", lambda name: fake.table)
    yield fake
    settings.cache_clear()


def serve(monkeypatch: pytest.MonkeyPatch, routes: Mapping[str, httpx.Response]) -> None:
    """Answer the downloads from ``routes`` (URL -> response); any other URL is a 404."""
    transport = httpx.MockTransport(lambda request: routes.get(str(request.url), httpx.Response(404)))
    monkeypatch.setattr(readiness.httpx, "Client", functools.partial(httpx.Client, transport=transport))


def ad_copy(fake: Fakes, seconds: float = 30.03) -> dict[str, Any]:
    """Write the pipeline's CDN copy of the creative's encode to the media bucket; returns the ingest's output."""
    main = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for n, rendition in enumerate(LADDER.video, start=1):
        peak, average = LADDER.nominal_bandwidth(rendition)
        main += [
            f'#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={average},CODECS="avc1.64001f,{AUDIO_CODEC}",'
            f"RESOLUTION={rendition.width}x{rendition.height},FRAME-RATE=29.970",
            f"main_{n}.m3u8",
        ]
        segments, left = [], seconds
        while left > 1e-9:
            segments += [f"#EXTINF:{min(2.002, left):.3f},", f"{CDN}/assets/{CRID}/seg_{len(segments)}.ts"]
            left -= min(2.002, left)
        fake.s3.objects[MEDIA, f"{AD_PREFIX}main_{n}.m3u8"] = "\n".join(["#EXTM3U", *segments, ""]).encode()
    fake.s3.objects[MEDIA, f"{AD_PREFIX}main.m3u8"] = "\n".join([*main, ""]).encode()
    return {
        "asset_id": CRID,
        "duration_ms": round(seconds * 1000),
        "creative": {"creative_id": CREATIVE_ID, "ad_prefix": AD_PREFIX, "hls_uri": f"{CDN}/{AD_PREFIX}main.m3u8"},
    }


def register(source: str = UPLOAD) -> dict[str, Any]:
    return readiness.received(
        readiness.Registration(crid=CRID, advertiser="Acme Outdoor", adomain=ADOMAIN, source=source, by="test"),
        "exec-1",
    )


def history(fake: Fakes) -> list[str]:
    return [entry["state"] for entry in fake.table.items[CRID]["history"]]


def upload(fake: Fakes) -> None:
    fake.s3.objects[QUARANTINE, source_key(CRID)] = MP4


# --- the happy path --------------------------------------------------------------------------------------------------


def test_a_registered_upload_goes_from_received_to_ready_and_the_own_book_is_told(fakes: Fakes) -> None:
    upload(fakes)
    register()
    assert readiness.fetch(CRID) == {"bucket": QUARANTINE, "key": source_key(CRID)}

    assert readiness.moderate(CRID, None) == {"status": "IN_PROGRESS", "job": "job-1"}
    [started] = fakes.rekognition.started
    assert started["Video"] == {"S3Object": {"Bucket": QUARANTINE, "Name": source_key(CRID)}}
    assert started["MinConfidence"] == 80
    fakes.rekognition.pages = [{"JobStatus": "IN_PROGRESS"}, {"JobStatus": "SUCCEEDED", "ModerationLabels": []}]
    assert readiness.moderate(CRID, "job-1")["status"] == "IN_PROGRESS"
    assert readiness.moderate(CRID, "job-1") == {"status": "SUCCEEDED", "state": "APPROVED", "job": "job-1"}

    request = readiness.conditioning(CRID, "exec-1")
    assert request == {
        "source_uri": UPLOAD,
        "kind": "ad",
        "asset_id": f"{CRID}-{digest('exec-1')}",
        "ladder_version": LADDER.version,
        "correlation_id": "exec-1-ingest",
        "metadata": {"crid": CRID, "adomain": ADOMAIN},
        "cue_points_ms": [],
    }
    assert readiness.conform(CRID, ad_copy(fakes)) == {"crid": CRID, "state": "CONDITIONED"}
    assert readiness.publish(CRID, "exec-1") == {"crid": CRID, "ready": [CRID]}

    assert history(fakes) == ["RECEIVED", "APPROVED", "CONDITIONING", "CONDITIONED", "READY"]
    ready = ReadyCreatives.model_validate_json(fakes.s3.objects[CONFIG, READY_KEY])
    [creative] = ready.creatives
    assert (ready.ladder_version, creative.crid, creative.adomain) == (LADDER.version, CRID, ADOMAIN)
    assert (creative.creative_id, creative.duration_ms, creative.height) == (CREATIVE_ID, 30030, 1080)
    assert fakes.lambda_.invoked == [{"FunctionName": PUBLISHER, "InvocationType": "Event", "Payload": b"{}"}]


def test_the_handler_dispatches_each_step_and_refuses_an_unknown_one(fakes: Fakes) -> None:
    upload(fakes)
    event = {
        "step": "received",
        "execution": "exec-1",
        "input": {"action": "register", "crid": CRID, "advertiser": "Acme", "adomain": ADOMAIN, "source": UPLOAD},
    }
    assert readiness.handler(event, Context()) == {"crid": CRID}
    with pytest.raises(ValueError, match="unknown step"):
        readiness.handler({"step": "nope"}, Context())


@dataclass
class Context:
    function_name: str = "fastm-creatives-readiness"
    function_version: str = "$LATEST"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-creatives-readiness"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-creatives-readiness"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 60_000


# --- moderation, decisions and conformance ---------------------------------------------------------------------------


def labelled(*labels: tuple[str, float]) -> dict[str, Any]:
    return {
        "JobStatus": "SUCCEEDED",
        "ModerationLabels": [
            {"Timestamp": 1000 * n, "ModerationLabel": {"Name": name, "Confidence": confidence, "ParentName": ""}}
            for n, (name, confidence) in enumerate(labels)
        ],
    }


def test_a_flagged_creative_waits_for_a_person_who_approves_or_rejects_it(fakes: Fakes) -> None:
    upload(fakes)
    register()
    readiness.fetch(CRID)
    fakes.rekognition.pages = [labelled(("Alcohol", 91.24), ("Alcohol", 85.0), ("Smoking", 40.0))]
    assert readiness.moderate(CRID, "job-1") == {"status": "SUCCEEDED", "state": "REVIEW", "job": "job-1"}
    record = fakes.table.items[CRID]
    assert record["moderation"]["labels"] == {"Alcohol": Decimal("91.2")}
    assert record["history"][-1]["note"] == "Alcohol 91%"

    decision = readiness.Decision(action="approve", crid=CRID, by="ops", note="brand-safe for this stage")
    assert readiness.decide(decision, "exec-2") == {"crid": CRID, "state": "APPROVED", "next": "condition"}
    with pytest.raises(TransitionError, match="applies to a creative REVIEW"):
        readiness.decide(readiness.Decision(action="reject", crid=CRID, by="ops"), "exec-3")


def test_with_moderation_off_a_creative_is_approved(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREATIVES_MODERATION", "false")
    settings.cache_clear()
    upload(fakes)
    register()
    assert readiness.moderate(CRID, None) == {"status": "SUCCEEDED", "state": "APPROVED", "job": None}
    assert fakes.rekognition.started == []


def test_an_off_grid_encode_is_rejected_with_its_problem(fakes: Fakes) -> None:
    upload(fakes)
    register()
    readiness.fetch(CRID)
    fakes.rekognition.pages = [labelled()]
    readiness.moderate(CRID, "job-1")
    readiness.conditioning(CRID, "exec-1")
    assert readiness.conform(CRID, ad_copy(fakes, seconds=20.02))["state"] == "REJECTED"
    assert "is not a whole number of 30030 ms ad slots" in fakes.table.items[CRID]["history"][-1]["note"]


def test_suspend_and_resume_withdraw_and_republish_a_ready_creative(fakes: Fakes) -> None:
    fakes.table.items[CRID] = {
        "crid": CRID,
        "state": "READY",
        "history": [],
        "ladder_version": LADDER.version,
        "creative_id": CREATIVE_ID,
        "hls_uri": f"{CDN}/{AD_PREFIX}main.m3u8",
        "duration_ms": 30030,
        "width": 1920,
        "height": 1080,
        "advertiser": "Acme",
        "adomain": ADOMAIN,
        "ready_at": "2026-11-07T10:00:00.000+00:00",
    }
    assert readiness.decide(readiness.Decision(action="suspend", crid=CRID, by="ops"), "x")["next"] == "publish"
    assert readiness.publish(CRID, "x")["ready"] == []
    readiness.decide(readiness.Decision(action="resume", crid=CRID, by="ops"), "y")
    assert readiness.publish(CRID, "y")["ready"] == [CRID]
    assert len(fakes.lambda_.invoked) == 2


# --- registration and failures ---------------------------------------------------------------------------------------


def test_a_creative_is_registered_once_unless_it_failed(fakes: Fakes) -> None:
    upload(fakes)
    register()
    with pytest.raises(TransitionError, match="RECEIVED cannot become RECEIVED"):
        register()
    error = {"Error": "SourceError", "Cause": "no such file"}
    assert readiness.failed({"crid": CRID}, "exec-1", error) == {"error": "SourceError", "cause": "no such file"}
    assert fakes.table.items[CRID]["state"] == "FAILED"
    register()  # registered again after the failure
    assert history(fakes) == ["RECEIVED", "FAILED", "RECEIVED"]


def test_a_failure_leaves_a_creative_alone_where_failed_is_not_allowed(fakes: Fakes) -> None:
    fakes.table.items[CRID] = {"crid": CRID, "state": "READY", "history": []}
    readiness.failed({"crid": CRID}, "exec-1", {"Error": "X", "Cause": "y"})
    assert fakes.table.items[CRID]["state"] == "READY"
    assert readiness.failed({}, "exec-1", {}) == {"error": "ReadinessFailed", "cause": ""}


def test_a_concurrent_change_fails_the_write(fakes: Fakes) -> None:
    upload(fakes)
    register()
    record = readiness.item(CRID)
    fakes.table.items[CRID]["state"] = "FAILED"  # someone else moved it
    with pytest.raises(ClientError):
        readiness._move(record, State.APPROVED, by="moderation")


# --- sources ---------------------------------------------------------------------------------------------------------

VAST = (
    '<VAST version="4.2"><Ad id="a"><InLine><AdSystem>x</AdSystem><AdTitle>t</AdTitle><Creatives><Creative>'
    "<Linear><Duration>00:00:30.030</Duration><MediaFiles>"
    '<MediaFile delivery="progressive" type="video/mp4" width="1920" height="1080">'
    "<![CDATA[https://ads.example/spot.mp4]]></MediaFile></MediaFiles></Linear></Creative></Creatives></InLine></Ad>"
    "</VAST>"
)


@pytest.mark.parametrize(
    ("source", "routes"),
    [
        ("https://ads.example/spot.mp4", {"https://ads.example/spot.mp4": httpx.Response(200, content=MP4)}),
        (
            "https://ads.example/vast.xml",
            {
                "https://ads.example/vast.xml": httpx.Response(200, text=VAST),
                "https://ads.example/spot.mp4": httpx.Response(200, content=MP4),
            },
        ),
        (
            "https://ads.example/moved.mp4",
            {
                "https://ads.example/moved.mp4": httpx.Response(302, headers={"Location": "/spot.mp4"}),
                "https://ads.example/spot.mp4": httpx.Response(200, content=MP4),
            },
        ),
    ],
)
def test_an_mp4_or_vast_url_is_copied_to_quarantine(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, source: str, routes: Mapping[str, httpx.Response]
) -> None:
    serve(monkeypatch, routes)
    register(source)
    assert readiness.fetch(CRID) == {"bucket": QUARANTINE, "key": source_key(CRID)}
    assert fakes.s3.objects[QUARANTINE, source_key(CRID)] == MP4
    assert fakes.table.items[CRID]["mezzanine"] == "https://ads.example/spot.mp4"
    assert fakes.table.items[CRID]["object"] == UPLOAD


@pytest.mark.parametrize(
    ("source", "routes", "message"),
    [
        ("https://ads.example/page", {"https://ads.example/page": httpx.Response(200, text="<html/>")}, "neither"),
        (
            "https://ads.example/moved.mp4",
            {"https://ads.example/moved.mp4": httpx.Response(302, headers={"Location": "http://ads.example/a"})},
            "not an https URL",
        ),
    ],
)
def test_a_source_that_is_not_a_usable_https_mp4_is_refused(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, source: str, routes: Mapping[str, httpx.Response], message: str
) -> None:
    serve(monkeypatch, routes)
    register(source)
    with pytest.raises(readiness.SourceError, match=message):
        readiness.fetch(CRID)


def test_an_upload_must_be_in_the_creatives_own_quarantine_directory(fakes: Fakes) -> None:
    fakes.s3.objects[QUARANTINE, "incoming/cr-000000000000/source.mp4"] = MP4
    register(f"s3://{QUARANTINE}/incoming/cr-000000000000/source.mp4")
    with pytest.raises(readiness.SourceError, match="must be under"):
        readiness.fetch(CRID)


def test_the_ready_document_is_what_the_publisher_and_the_sync_read(fakes: Fakes) -> None:
    fakes.table.items = {
        crid: {
            "crid": crid,
            "state": state,
            "ladder_version": ladder,
            "creative_id": f"{crid}-x",
            "hls_uri": f"{CDN}/ads/{crid}/main.m3u8",
            "duration_ms": 30030,
            "width": 1920,
            "height": 1080,
            "advertiser": "Acme",
            "adomain": ADOMAIN,
            "ready_at": at,
        }
        for crid, state, ladder, at in (
            ("cr-000000000002", "READY", LADDER.version, "2026-11-07T10:00:02Z"),
            ("cr-000000000001", "READY", LADDER.version, "2026-11-07T10:00:01Z"),
            ("cr-000000000003", "READY", "standard@0000000000", "2026-11-07T10:00:00Z"),
            ("cr-000000000004", "SUSPENDED", LADDER.version, "2026-11-07T09:00:00Z"),
        )
    }
    ready = readiness.ready_creatives(LADDER.version)
    assert [c.crid for c in ready.creatives] == ["cr-000000000001", "cr-000000000002"]
    assert json.loads(ready.model_dump_json())["v"] == 1
