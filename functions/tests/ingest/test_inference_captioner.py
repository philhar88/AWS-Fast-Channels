"""The Elemental Inference captioner's steps against an in-memory ingest bucket, a fake feed and stubbed APIs."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from fast_channels.core import inference as core
from fast_channels.core.inference import Chunk
from fast_channels.functions.captioners import inference
from fast_channels.functions.captioners.inference import Feed, FeedQuotaExceededError, NoSpeechError

WINDOWS = json.loads((Path(__file__).parents[3] / "core/tests/data/inference/tears-of-steel-windows.json").read_text())
REQUEST = {
    "asset_id": "tos-1a2b3c",
    "language": "en",
    "correlation_id": "run-1",
    "source_uri": "s3://ingest/films/tos.mov",
    "output_uri": "s3://ingest/captions/tos-1a2b3c/run-1/en",
}
PREFIX = "work/tos-1a2b3c/run-1/inference/"
SEGMENT = b"\x00\x00\x00\x10stypmsdh\x00\x00\x00\x00" + b"\x00\x00\x00\x08moof"


@pytest.fixture
def bucket(store: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    memory = store()
    memory.bucket = "ingest"
    monkeypatch.setattr(inference, "S3Store", lambda name: memory)
    return memory


def proxy_objects(segments: int) -> dict[str, bytes]:
    playlist = "#EXTM3U\n" + "#EXTINF:1.000,\nx.cmfv\n" * segments
    objects = {
        f"{PREFIX}proxy_v.m3u8": playlist.encode(),
        f"{PREFIX}proxy_a.m3u8": (playlist + "#EXTINF:0.1,\ny\n").encode(),
    }
    for modifier, extension in (("_v", "cmfv"), ("_a", "cmfa")):
        objects[f"{PREFIX}proxy{modifier}init.{extension}"] = b"init" + modifier.encode()
        objects |= {f"{PREFIX}proxy{modifier}_{n:09d}.{extension}": SEGMENT for n in range(1, segments + 1)}
    return objects


def test_proxy_names_its_job_and_place(bucket: Any) -> None:
    prepared = inference.proxy(REQUEST, "exec-1")
    assert prepared["prefix"] == PREFIX
    settings = prepared["job"]["Settings"]
    assert settings["Inputs"][1]["FileInput"] == REQUEST["source_uri"]
    destination = settings["OutputGroups"][0]["OutputGroupSettings"]["CmafGroupSettings"]["Destination"]
    assert destination == f"s3://ingest/{PREFIX}proxy"
    assert prepared["job"]["UserMetadata"] == {"app": "fast", "correlation_id": "run-1", "step": "proxy"}
    with pytest.raises(inference.UnsupportedLanguageError, match="ja"):
        inference.proxy(REQUEST | {"language": "ja"}, "exec-1")


def test_plan_counts_the_segments_both_tracks_have(bucket: Any) -> None:
    bucket.objects |= proxy_objects(169)
    assert inference.plan({"prefix": PREFIX}) == core.plan_chunks(169)
    with pytest.raises(FileNotFoundError, match=r"proxy_v\.m3u8"):
        inference.plan({"prefix": "work/none/"})


@dataclass
class FakeFeed:
    puts: list[tuple[str, str, bytes]] = field(default_factory=list)
    queried: list[tuple[int, int]] = field(default_factory=list)

    def put(self, stream: str, part: str, body: bytes) -> None:
        self.puts.append((stream, part, body))

    def subtitles(self, start_ms: int, end_ms: int) -> str:
        self.queried.append((start_ms, end_ms))
        return next((w["ttml"] for w in WINDOWS if w["startPts"] == start_ms), "")


def test_relay_paces_the_chunk_marks_its_end_and_keeps_its_own_words(
    bucket: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket.objects |= proxy_objects(75)
    fake = FakeFeed()
    opened: list[tuple[str, str, dict[str, str]]] = []

    @contextmanager
    def opened_feed(name: str, language: str, tags: dict[str, str]) -> Iterator[FakeFeed]:
        opened.append((name, language, tags))
        yield fake

    monkeypatch.setattr(inference, "opened_feed", opened_feed)
    monkeypatch.setattr(inference.time, "sleep", lambda _: None)
    chunk = Chunk(index=1, first=1, last=75, start_ms=35_000, end_ms=60_000)

    result = inference.relay(REQUEST, {"prefix": PREFIX}, chunk, "exec-1")

    [(name, language, tags)] = opened
    assert (name, language) == ("exec-1-1", "eng")
    assert tags == {
        "fast:app": "fast",
        "fast:owner": "pipeline",
        "fast:correlation-id": "run-1",
        "fast:execution": "exec-1",
    }
    assert [part for _, part, _ in fake.puts[:2]] == ["InitializationSegment", "InitializationSegment"]
    segments = fake.puts[2:]
    assert [(s, p) for s, p, _ in segments[:2]] == [
        (core.VIDEO_STREAM, "Segment(000000001)"),
        (core.AUDIO_STREAM, "Segment(000000001)"),
    ]
    assert len(segments) == 150 and all(b"lmsg" not in body for _, _, body in segments[:-2])
    assert all(body.endswith(b"lmsg" + b"\x00\x00\x00\x08moof") for _, _, body in segments[-2:])
    assert fake.queried == [(25_000, 55_000), (55_000, 60_000)]  # 10 s of context before the chunk's own range
    stored = json.loads(bucket.objects[result["key"]])
    assert result == {"index": 1, "key": f"{PREFIX}chunk-0001.json", "words": len(stored)}
    assert stored and all(35_000 <= w["start_ms"] < 60_000 for w in stored)
    text = " ".join(w["text"] for w in stored)
    assert text.startswith("freaked out by its. All right, fine.") and "Look, Celia" not in text


def test_stitch_writes_one_ttml_on_the_source_timeline(bucket: Any) -> None:
    words = [
        {"start_ms": 9_000, "end_ms": 9_400, "text": "Hello"},
        {"start_ms": 9_400, "end_ms": 10_500, "text": "there."},
    ]
    bucket.objects[f"{PREFIX}chunk-0000.json"] = json.dumps(words).encode()
    bucket.objects[f"{PREFIX}chunk-0001.json"] = b"[]"
    chunks = [{"key": f"{PREFIX}chunk-0000.json"}, {"key": f"{PREFIX}chunk-0001.json"}]

    result = inference.stitch(REQUEST, chunks)

    assert result == {"uri": f"{REQUEST['output_uri']}.ttml", "format": "ttml", "engine": "elemental-inference"}
    document = bucket.objects["captions/tos-1a2b3c/run-1/en.ttml"].decode()
    assert '<p begin="00:00:01.000" end="00:00:02.500" region="bottom">' in document and "Hello there." in document
    bucket.objects[f"{PREFIX}chunk-0000.json"] = b"[]"
    with pytest.raises(NoSpeechError):
        inference.stitch(REQUEST, chunks)


def test_a_feed_is_created_associated_and_always_deleted(stubs: Any) -> None:
    created = {
        "arn": "arn:x",
        "name": "exec-1-0",
        "id": "feed1",
        "dataEndpoints": ["abc.data.example"],
        "outputs": [],
        "status": "AVAILABLE",
    }
    stubs["elementalinference"].add_response("create_feed", created)
    stubs["elementalinference"].add_response("associate_feed", {"arn": "arn:x", "id": "feed1"})
    stubs["elementalinference"].add_response("delete_feed", {"arn": "arn:x", "id": "feed1", "status": "DELETING"})
    with inference.opened_feed("exec-1-0", "eng", {"fast:app": "fast"}) as feed:
        assert (feed.id, feed.endpoint, feed.region) == ("feed1", "https://abc.data.example", "us-east-1")

    stubs["elementalinference"].add_response("create_feed", created)
    stubs["elementalinference"].add_client_error(
        "associate_feed",
        "ServiceQuotaExceededException",
        "You've reached the quota of active feeds",
        http_status_code=402,
    )
    stubs["elementalinference"].add_response("delete_feed", {"arn": "arn:x", "id": "feed1", "status": "DELETING"})
    with pytest.raises(FeedQuotaExceededError, match="quota of active feeds"), inference.opened_feed("x", "eng", {}):
        pass


def test_the_data_plane_is_signed_and_retries_until_the_association_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inference.time, "sleep", lambda _: None)
    seen: list[httpx.Request] = []
    answers = iter(
        [httpx.Response(409, json={"message": "Feed is not associated with a resource."}), httpx.Response(200, json={})]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return next(answers)

    feed = Feed("feed1", "https://abc.data.example", "us-east-1", httpx.Client(transport=httpx.MockTransport(handle)))
    feed.put(core.VIDEO_STREAM, "Segment(000000001)", b"media")
    assert len(seen) == 2
    headers = seen[-1].headers
    assert (
        headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        and "/us-east-1/elemental-inference/" in headers["Authorization"]
    )
    assert headers["X-Amz-Content-SHA256"] == inference._sha256(b"media")
    assert seen[-1].url.path == "/v1/feed/feed1/input/0/media/Streams(default-video.cmfv)/Segment(000000001)"

    empty = Feed(
        "feed1",
        "https://abc.data.example",
        "us-east-1",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"message": "no media"}))),
    )
    assert empty.subtitles(0, 30_000) == ""
    failing = Feed(
        "feed1",
        "https://abc.data.example",
        "us-east-1",
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"message": "Invalid media path."}))
        ),
    )
    with pytest.raises(RuntimeError, match=r"400 .*Invalid media path"):
        failing.put(core.AUDIO_STREAM, "Segment(1)", b"x")


def test_cleanup_deletes_only_the_executions_feeds(stubs: Any) -> None:
    feeds = [
        {"arn": "arn:a", "id": "f1", "name": "exec-1-0", "status": "ACTIVE"},
        {"arn": "arn:b", "id": "f2", "name": "exec-1-3", "status": "ACTIVE"},
        {"arn": "arn:c", "id": "f3", "name": "exec-10-0", "status": "ACTIVE"},
    ]
    stubs["elementalinference"].add_response("list_feeds", {"feeds": feeds})
    for feed_id in ("f1", "f2"):
        stubs["elementalinference"].add_response(
            "delete_feed", {"arn": "arn", "id": feed_id, "status": "DELETING"}, {"id": feed_id}
        )
    assert inference.cleanup("exec-1") == ["f1", "f2"]
    assert inference.feed_name("arn:aws:states:x:exec/1", 2) == "arn-aws-states-x-exec-1-2"
