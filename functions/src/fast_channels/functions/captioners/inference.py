"""Steps of the Elemental Inference captioner (``infra.constructs.encoding.captioners.InferenceCaptioner``).

The state machine runs ``proxy`` -> the proxy's MediaConvert job -> ``plan`` -> ``relay`` per chunk (a Map, as many
at once as the account may have active feeds) -> ``stitch``; ``cleanup`` deletes the feeds of a failed execution.
``core.inference`` holds the media and caption logic; this module does the AWS calls.

* ``proxy``: the job settings and the S3 stem of the proxy (``work/<asset>/<correlation id>/inference/proxy``).
* ``plan``: count the proxy's segments (its HLS playlists) and split them into chunks.
* ``relay``: a fresh feed (tagged with the deployment and the execution), its init segments, then video and audio
  segment ``n`` of the chunk every :data:`PACE_S` (about 1.4x real time, under the 1.5 requests per second per
  stream that PutMedia allows), the last one marked ``lmsg``; then, after :data:`SETTLE_S`, every window of the
  chunk with GetMetadata, and the words the chunk is authoritative for, written to S3. The feed is deleted however
  the step ends. ``FeedQuotaExceededError`` (the account's active feeds are in use) is retried by the state machine.
* ``stitch``: every chunk's words, as pop-on captions on the source timeline, in one IMSC1 TTML file at the
  contract's ``output_uri`` + ``.ttml``. A title without speech fails with ``NoSpeechError``: an empty track then.
"""

import hashlib
import json
import re
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import boto3
import httpx
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from fast_channels.core import inference
from fast_channels.core.captions import CaptionFormat
from fast_channels.core.inference import AUDIO_STREAM, PROXY_AUDIO, PROXY_VIDEO, VIDEO_STREAM, Chunk, Word
from fast_channels.core.ingest import WORK_PREFIX
from fast_channels.core.mp4 import with_compatible_brand
from fast_channels.core.naming import digest
from fast_channels.core.tags import Owner, resource_tags
from fast_channels.functions import aws
from fast_channels.functions.ingest.storage import S3Store, read_text
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

ENGINE: Final = "elemental-inference"
SERVICE: Final = "elemental-inference"
"""SigV4 signing name of the data plane."""
OUTPUT: Final = "subtitles"
PACE_S: Final = 0.7
SETTLE_S: Final = 20.0
"""After the last PutMedia, every window was final within one sweep of 19 s (docs/captions.md)."""
CONTEXT_MS: Final = 10_000
"""Words before a chunk's own range that are read too, so the first roll-up lines have their context."""
PREFETCH: Final = 8
NAME_LENGTH: Final = 128
TRACKS: Final = ((VIDEO_STREAM, PROXY_VIDEO, "cmfv"), (AUDIO_STREAM, PROXY_AUDIO, "cmfa"))
"""The two CMAF Ingest streams of a feed: name, proxy output NameModifier, segment extension."""

logger = Logger()
metrics = Metrics()


class FeedQuotaExceededError(RuntimeError):
    """The account has no active feed to spare (``Active feeds per account``); retried with backoff."""


class NoSpeechError(RuntimeError):
    """The feed transcribed no words: the title has no speech in this language."""


class UnsupportedLanguageError(ValueError):
    """Smart subtitles do not transcribe the requested language."""


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> Any:
    """``{"step": ..., "request": <captioner input>, ...}``: see the module docstring."""
    config = settings()
    config.require("ingest_bucket", "prefix")
    request = event["request"]
    logger.set_correlation_id(request["correlation_id"])
    logger.append_keys(asset_id=request["asset_id"], language=request["language"])
    match event["step"]:
        case "proxy":
            return proxy(request, event["execution"])
        case "plan":
            return [asdict(chunk) for chunk in plan(event["proxy"])]
        case "relay":
            return relay(request, event["proxy"], Chunk(**event["chunk"]), event["execution"])
        case "stitch":
            return stitch(request, event["chunks"])
        case "cleanup":
            return {"deleted": cleanup(event["execution"])}
        case step:
            raise ValueError(f"unknown step {step!r}")


def proxy(request: Mapping[str, Any], execution: str) -> dict[str, Any]:
    """The proxy job of ``request`` and where it writes (see ``core.inference.proxy_settings``)."""
    if inference.inference_language(request["language"]) is None:
        raise UnsupportedLanguageError(f"smart subtitles do not transcribe {request['language']}")
    prefix = f"{WORK_PREFIX}{request['asset_id']}/{request['correlation_id']}/inference/"
    destination = f"s3://{settings().ingest_bucket}/{prefix}proxy"
    return {
        "prefix": prefix,
        "job": {
            "Settings": inference.proxy_settings(source_uri=request["source_uri"], destination=destination),
            "UserMetadata": {"app": settings().app, "correlation_id": request["correlation_id"], "step": "proxy"},
            "ClientRequestToken": digest(f"{execution}#proxy", 32),
        },
    }


def plan(prepared: Mapping[str, Any]) -> list[Chunk]:
    """The chunks of the proxy at ``prepared["prefix"]``: as many segments as both of its playlists have."""
    store = S3Store(settings().ingest_bucket)
    counts = []
    for modifier in (PROXY_VIDEO, PROXY_AUDIO):
        key = f"{prepared['prefix']}proxy{modifier}.m3u8"
        if (text := read_text(store, key)) is None:
            raise FileNotFoundError(f"the proxy job wrote no {key}")
        counts.append(sum(line.startswith("#EXTINF:") for line in text.splitlines()))
    chunks = inference.plan_chunks(min(counts))
    logger.info("proxy planned", extra={"segments": counts, "chunks": len(chunks)})
    return chunks


@dataclass(frozen=True, slots=True)
class Feed:
    """One smart-subtitles feed and its data plane (SigV4-signed HTTPS to the feed's data endpoint)."""

    id: str
    endpoint: str
    region: str
    http: httpx.Client

    def put(self, stream: str, part: str, body: bytes) -> None:
        url = f"{self.endpoint}/v1/feed/{self.id}/input/0/media/{stream}/{part}"
        self._send("PUT", url, body, {"Content-Type": "application/octet-stream"})

    def subtitles(self, start_ms: int, end_ms: int) -> str:
        """The TTML of a window, empty when the feed has nothing for it."""
        body = json.dumps(
            {
                "outputName": OUTPUT,
                "timeSpecification": {"ptsBased": {"startPts": start_ms, "endPts": end_ms, "timescale": 1000}},
                "parameters": {"subtitling": {"format": "TTML"}},
            }
        ).encode()
        headers = {"Content-Type": "application/json", "x-amzn-elemental-inference-skip-poll": "true"}
        response = self._send("POST", f"{self.endpoint}/v1/feed/{self.id}/input/0/metadata", body, headers, empty=404)
        items = response.json().get("items", []) if response.status_code == httpx.codes.OK else []
        return "".join(str(item.get("metadata", {}).get("subtitling", {}).get("ttml", "")) for item in items)

    def _send(
        self, method: str, url: str, body: bytes, headers: dict[str, str], *, empty: int | None = None
    ) -> httpx.Response:
        """Send a signed request; retry what the data plane retries (409 until the association propagates, 429 and
        5xx) for about 20 s."""
        for attempt in range(8):
            signed = AWSRequest(
                method=method, url=url, data=body, headers=headers | {"X-Amz-Content-SHA256": _sha256(body)}
            )
            SigV4Auth(_credentials(), SERVICE, self.region).add_auth(signed)
            response = self.http.request(method, url, content=body, headers=dict(signed.headers.items()))
            if response.is_success or response.status_code == empty:
                return response
            if response.status_code not in {409, 429} and response.status_code < 500:
                break
            time.sleep(min(0.25 * 2**attempt, 5))
        raise RuntimeError(f"{method} {urlsplit(url).path} -> {response.status_code} {response.text[:300]}")


@contextmanager
def opened_feed(name: str, language: str, tags: Mapping[str, str]) -> Iterator[Feed]:
    """A new, associated feed with one subtitles output; deleted on exit."""
    client = aws.client("elementalinference")
    subtitling: dict[str, Any] = {"language": language, "aspectRatio": {"width": 16, "height": 9}}
    if dictionary := settings().inference_dictionary:
        subtitling["dictionary"] = dictionary
    output = {"name": OUTPUT, "status": "ENABLED", "outputConfig": {"subtitling": subtitling}}
    created = client.create_feed(name=name, outputs=[output], tags=dict(tags))
    try:
        try:
            client.associate_feed(id=created["id"], associatedResourceName=name, outputs=[])
        except ClientError as error:
            if error.response["Error"]["Code"] == "ServiceQuotaExceededException":
                raise FeedQuotaExceededError(error.response["Error"].get("Message", "")) from None
            raise
        endpoint = created["dataEndpoints"][0].rstrip("/")
        with httpx.Client(timeout=httpx.Timeout(30.0)) as http:
            url = endpoint if endpoint.startswith("https://") else f"https://{endpoint}"
            yield Feed(created["id"], url, client.meta.region_name, http)
    finally:
        with suppress(ClientError):
            client.delete_feed(id=created["id"])
        logger.info("feed deleted", extra={"feed": created["id"]})


def relay(request: Mapping[str, Any], prepared: Mapping[str, Any], chunk: Chunk, execution: str) -> dict[str, Any]:
    """Relay one chunk to a fresh feed and store the words it is authoritative for (see the module docstring)."""
    config = settings()
    store = S3Store(config.ingest_bucket)
    stem = f"{prepared['prefix']}proxy"
    language = inference.inference_language(request["language"])
    if language is None:
        raise UnsupportedLanguageError(f"smart subtitles do not transcribe {request['language']}")
    name = feed_name(execution, chunk.index)
    tags = resource_tags(config.prefix, Owner.PIPELINE, correlation_id=request["correlation_id"], execution=execution)
    logger.append_keys(chunk=chunk.index)
    started = time.monotonic()
    with opened_feed(name, language, tags) as feed, ThreadPoolExecutor(PREFETCH) as pool:
        for stream, modifier, extension in TRACKS:
            feed.put(stream, "InitializationSegment", _required(store, f"{stem}{modifier}init.{extension}"))
        numbers = range(chunk.first, chunk.last + 1)
        video, audio = (
            pool.map(lambda n, m=modifier, e=extension: _required(store, f"{stem}{m}_{n:09d}.{e}"), numbers)
            for _, modifier, extension in TRACKS
        )
        due = time.monotonic()
        for n, *segments in zip(numbers, video, audio, strict=True):
            last = n == chunk.last  # CMAF Ingest's end of the session
            video_segment, audio_segment = (with_compatible_brand(s, "lmsg") if last else s for s in segments)
            time.sleep(max(0.0, due - time.monotonic()))
            feed.put(VIDEO_STREAM, f"Segment({n:09d})", video_segment)
            feed.put(AUDIO_STREAM, f"Segment({n:09d})", audio_segment)
            due += PACE_S
        relayed = time.monotonic() - started
        time.sleep(SETTLE_S)
        pushed_end = chunk.last * inference.SEGMENT_MS
        start = max((chunk.first - 1) * inference.SEGMENT_MS, chunk.start_ms - CONTEXT_MS)
        shots = [
            shot
            for begin, end in inference.windows(start, min(chunk.end_ms, pushed_end))
            for shot in inference.snapshots(feed.subtitles(begin, end), offset_ms=begin)
        ]
    own = [w for w in inference.words(shots) if chunk.start_ms <= w.start_ms < chunk.end_ms]
    key = f"{prepared['prefix']}chunk-{chunk.index:04d}.json"
    store.write(key, json.dumps([asdict(w) for w in own]).encode())
    seconds = chunk.last - chunk.first + 1
    metrics.add_metric(name="InferenceMediaSeconds", unit=MetricUnit.Seconds, value=seconds)
    logger.info(
        "chunk relayed",
        extra={
            "segments": seconds,
            "relay_s": round(relayed, 1),
            "speed": round(seconds / relayed, 2),
            "words": len(own),
        },
    )
    return {"index": chunk.index, "key": key, "words": len(own)}


def stitch(request: Mapping[str, Any], chunks: list[Mapping[str, Any]]) -> dict[str, str]:
    """One caption file from every chunk's words (the contract's output)."""
    store = S3Store(settings().ingest_bucket)
    stream = sorted(
        (Word(**word) for chunk in chunks for word in json.loads(_required(store, chunk["key"]))),
        key=lambda w: w.start_ms,
    )
    if not stream:
        raise NoSpeechError(f"no {request['language']} speech was transcribed")
    entries = inference.captions(stream, offset_ms=inference.PREROLL_MS)
    bucket, _, key = str(request["output_uri"]).removeprefix("s3://").partition("/")
    if bucket != store.bucket:
        raise ValueError(f"{request['output_uri']} is not in the ingest bucket")
    store.write(f"{key}.ttml", inference.ttml(entries, language=request["language"]).encode())
    metrics.add_metric(name="InferenceCaptions", unit=MetricUnit.Count, value=len(entries))
    logger.info("captions written", extra={"words": len(stream), "captions": len(entries)})
    return {"uri": f"{request['output_uri']}.ttml", "format": CaptionFormat.TTML.value, "engine": ENGINE}


def feed_name(execution: str, chunk: int) -> str:
    """``<execution>-<chunk>`` as a feed name (letters, digits, ``-`` and ``_``; at most 128)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", execution).strip("-_")
    return f"{safe[: NAME_LENGTH - 6]}-{chunk}"


def cleanup(execution: str) -> list[str]:
    """Delete the feeds of ``execution`` (see :func:`feed_name`) that a failed relay left."""
    client = aws.client("elementalinference")
    prefix = feed_name(execution, 0).removesuffix("0")
    pages = client.get_paginator("list_feeds").paginate()
    feeds = [feed["id"] for page in pages for feed in page.get("feeds", []) if feed["name"].startswith(prefix)]
    for feed_id in feeds:
        with suppress(ClientError):
            client.delete_feed(id=feed_id)
    logger.info("feeds cleaned up", extra={"feeds": feeds})
    return feeds


def _required(store: S3Store, key: str) -> bytes:
    if (body := store.read(key)) is None:
        raise FileNotFoundError(f"missing {key}")
    return body


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@cache
def _session() -> boto3.Session:
    return boto3.Session()


def _credentials() -> Any:
    """The function's (refreshable) credentials, frozen for one signature."""
    credentials = _session().get_credentials()
    if credentials is None:
        raise RuntimeError("no AWS credentials")
    return credentials.get_frozen_credentials()
