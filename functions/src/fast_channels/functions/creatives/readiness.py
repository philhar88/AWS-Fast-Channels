"""Steps of the readiness state machine (``<prefix>-creatives``): a creative from registration to READY.

OPTIMIZATION(aws): W-2 FR-3: MediaTailor has no creative-readiness API (register, pre-transcode per variant set,
query readiness), so the module conditions each creative itself, checks it against the ladder, and lets the own book
bid it only once it is READY (docs/creatives.md#workarounds-to-remove).

One function, dispatched on ``event["step"]``; every state change is a conditional write on the registry item's state
(``core.creatives.registry.TRANSITIONS``), with its history:

- ``received``: record a registration (:class:`~fast_channels.core.creatives.registry.Registration`) as RECEIVED;
- ``fetch``: copy its source into the quarantine bucket (``incoming/<crid>/source.mp4``). An upload is already there;
  an MP4 URL is downloaded; a VAST URL's best progressive MP4 is (``core.creatives.vast``);
- ``moderate``: Amazon Rekognition content moderation of the copy, started on the first call and polled after:
  APPROVED, or REVIEW when any label scores at least the stage's confidence (with moderation off: APPROVED);
- ``decide``: a person's decision (:class:`~fast_channels.core.creatives.registry.Decision`);
- ``conditioning``: CONDITIONING, and the ingest request the state machine runs as a child execution of the ingest
  pipeline (``kind: ad``, on the stage's default ladder);
- ``conform``: the ladder-conformance check of the encode's CDN copy (``core.creatives.conformance``): CONDITIONED,
  or REJECTED with the problems;
- ``publish``: READY, then the READY creatives to the PDS config bucket (``creatives/ready.json``) and a run of the
  snapshot publisher;
- ``failed``: FAILED, where the state allows it, with the error.
"""

import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx
from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key

from fast_channels.core.creatives.conformance import check
from fast_channels.core.creatives.names import STATE_INDEX, source_key
from fast_channels.core.creatives.registry import (
    DECIDED,
    TRANSITIONS,
    Action,
    Decision,
    ReadyCreatives,
    RegisteredCreative,
    Registration,
    State,
    TransitionError,
    check_transition,
)
from fast_channels.core.creatives.vast import VastError, mezzanine
from fast_channels.core.ingest import AssetKind, IngestRequest
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import parse_multivariant
from fast_channels.core.naming import digest
from fast_channels.functions import aws
from fast_channels.functions.media_library import update_request
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

REQUIRED: Final = (
    "creatives_table",
    "creatives_quarantine_bucket",
    "creatives_ladder",
    "media_bucket",
    "pds_config_bucket",
    "pds_creatives_key",
    "pds_publisher_function",
)
MAX_SOURCE: Final = 400 * 1024 * 1024
"""The largest source file copied (bytes)."""
MAX_REDIRECTS: Final = 5
TIMEOUT: Final = httpx.Timeout(30.0, read=60.0)
NEXT: Final[Mapping[Action, str]] = {
    Action.APPROVE: "condition",
    Action.REJECT: "done",
    Action.SUSPEND: "publish",
    Action.RESUME: "publish",
}
"""What the state machine does after a person's decision."""

logger = Logger()


class SourceError(ValueError):
    """A creative's source cannot be copied: not https, too large, not an MP4 or a usable VAST document."""


class ModerationError(RuntimeError):
    """Rekognition could not moderate the creative."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


# --- the registry ----------------------------------------------------------------------------------------------------


def item(crid: str) -> dict[str, Any] | None:
    """The registry item of ``crid`` (strongly consistent), or None."""
    found: dict[str, Any] | None = (
        aws.table(settings().creatives_table).get_item(Key={"crid": crid}, ConsistentRead=True).get("Item")
    )
    return found


def _item(crid: str) -> dict[str, Any]:
    if (found := item(crid)) is None:
        raise TransitionError(f"no creative {crid}")
    return found


def state_of(record: Mapping[str, Any] | None) -> State | None:
    return State(record["state"]) if record is not None else None


def move(
    crid: str,
    current: State | None,
    wanted: State,
    *,
    by: str,
    note: str = "",
    execution: str = "",
    history: list[dict[str, Any]] | None = None,
    values: Mapping[str, Any] | None = None,
) -> None:
    """Move ``crid`` from ``current`` to ``wanted`` (a conditional write: a concurrent change fails it), appending to
    its ``history`` and setting ``values``."""
    check_transition(current, wanted)
    at = now_iso()
    entry = {"state": wanted.value, "at": at, "by": by} | ({"note": note[:500]} if note else {})
    entry |= {"execution": execution} if execution else {}
    condition = Attr("crid").not_exists() if current is None else Attr("state").eq(current.value)
    aws.table(settings().creatives_table).update_item(
        **update_request(
            {"crid": crid},
            {**(values or {}), "state": wanted.value, "updated_at": at, "history": [*(history or []), entry]},
            ConditionExpression=condition,
        )
    )
    logger.info("creative moved", extra={"crid": crid, "from": current, "to": wanted, "by": by})


def _move(record: Mapping[str, Any], wanted: State, **kwargs: Any) -> None:
    move(record["crid"], state_of(record), wanted, history=list(record.get("history", [])), **kwargs)


# --- steps -----------------------------------------------------------------------------------------------------------


def received(registration: Registration, execution: str) -> dict[str, Any]:
    """Record the registration as RECEIVED (a new creative, or a FAILED one registered again)."""
    record = item(registration.crid)
    values = {
        "advertiser": registration.advertiser,
        "adomain": registration.adomain,
        "source": registration.source,
        "created_at": record["created_at"] if record else now_iso(),
    }
    move(
        registration.crid,
        state_of(record),
        State.RECEIVED,
        by=registration.by,
        execution=execution,
        history=list(record.get("history", [])) if record else [],
        values=values,
    )
    return {"crid": registration.crid}


def fetch(crid: str) -> dict[str, str]:
    """Copy the creative's source into the quarantine bucket; returns where it is."""
    config = settings()
    record = _item(crid)
    bucket, key, source = config.creatives_quarantine_bucket, source_key(crid), str(record["source"])
    match urlsplit(source).scheme:
        case "s3":
            key = _uploaded(source, bucket, crid)
            mezzanine_url = source
        case "https":
            mezzanine_url = _download_to(source, bucket, key)
        case scheme:
            raise SourceError(f"a source is s3:// or https://, not {scheme}://")
    aws.table(config.creatives_table).update_item(
        **update_request(
            {"crid": crid},
            {"object": f"s3://{bucket}/{key}", "mezzanine": mezzanine_url, "updated_at": now_iso()},
            ConditionExpression=Attr("state").eq(State.RECEIVED.value),
        )
    )
    return {"bucket": bucket, "key": key}


def _uploaded(source: str, bucket: str, crid: str) -> str:
    """The key of an upload to the quarantine bucket, which must be under the creative's own directory."""
    parts = urlsplit(source)
    key = parts.path.lstrip("/")
    if parts.netloc != bucket or not key.startswith(source_key(crid).rpartition("/")[0] + "/"):
        raise SourceError(f"an uploaded source must be under s3://{bucket}/{source_key(crid).rpartition('/')[0]}/")
    head = aws.client("s3").head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] > MAX_SOURCE:
        raise SourceError(f"the source is over {MAX_SOURCE} bytes")
    return key


@contextmanager
def _get(url: str) -> Iterator[httpx.Response]:
    """A streamed GET of ``url``, following https redirects only."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if urlsplit(url).scheme != "https":
                raise SourceError(f"not an https URL: {url[:200]}")
            with client.stream("GET", url, headers={"Accept": "video/mp4, application/xml, text/xml"}) as response:
                if response.is_redirect and (location := response.headers.get("location")):
                    url = str(response.url.join(location))
                    continue
                response.raise_for_status()
                yield response
                return
    raise SourceError(f"more than {MAX_REDIRECTS} redirects")


def _save(response: httpx.Response, path: Path) -> bytes:
    """Write the response body to ``path`` (refusing more than :data:`MAX_SOURCE`); returns its first bytes."""
    size, head = 0, b""
    with path.open("wb") as file:
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_SOURCE:
                raise SourceError(f"the source is over {MAX_SOURCE} bytes")
            if len(head) < 64:
                head += chunk[: 64 - len(head)]
            file.write(chunk)
    return head


def _is_mp4(head: bytes) -> bool:
    return head[4:8] == b"ftyp"


def _download_to(url: str, bucket: str, key: str) -> str:
    """Copy an MP4 at ``url``, or a VAST document's best progressive MP4, to ``bucket``/``key``; returns the MP4's
    URL."""
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "source"
        with _get(url) as response:
            head, url = _save(response, path), str(response.url)
        if not _is_mp4(head):
            try:
                media = mezzanine(path.read_text(errors="replace")).url
            except VastError as error:
                raise SourceError(f"neither an MP4 nor a usable VAST document: {error}") from None
            with _get(media) as response:
                head, url = _save(response, path), str(response.url)
            if not _is_mp4(head):
                raise SourceError(f"the VAST document's MP4 is not an MP4: {url[:200]}")
        aws.client("s3").upload_file(str(path), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    return url


def moderate(crid: str, job: str | None) -> dict[str, Any]:
    """Start the moderation (no ``job`` yet) or read its result: ``status`` IN_PROGRESS (with ``job``) or SUCCEEDED
    (with the ``state`` the creative moved to)."""
    config = settings()
    record = _item(crid)
    if not config.creatives_moderation:
        _move(record, State.APPROVED, by="readiness", note="moderation off")
        return {"status": "SUCCEEDED", "state": State.APPROVED.value}
    rekognition = aws.client("rekognition")
    if job is None:
        parts = urlsplit(str(record["object"]))
        started = rekognition.start_content_moderation(
            Video={"S3Object": {"Bucket": parts.netloc, "Name": parts.path.lstrip("/")}},
            MinConfidence=config.creatives_min_confidence,
        )
        return {"status": "IN_PROGRESS", "job": started["JobId"]}
    labels: dict[str, float] = {}
    request: dict[str, Any] = {"JobId": job, "SortBy": "NAME"}
    while True:
        page = rekognition.get_content_moderation(**request)
        match page["JobStatus"]:
            case "IN_PROGRESS":
                return {"status": "IN_PROGRESS", "job": job}
            case "FAILED":
                raise ModerationError(page.get("StatusMessage") or "moderation failed")
        for found in page.get("ModerationLabels", []):
            label = found["ModerationLabel"]
            labels[label["Name"]] = max(labels.get(label["Name"], 0.0), float(label["Confidence"]))
        if not (token := page.get("NextToken")):
            break
        request["NextToken"] = token
    flagged = {name: confidence for name, confidence in labels.items() if confidence >= config.creatives_min_confidence}
    state = State.REVIEW if flagged else State.APPROVED
    _move(
        record,
        state,
        by="moderation",
        note=", ".join(f"{name} {confidence:.0f}%" for name, confidence in sorted(flagged.items())),
        values={
            "moderation": {
                "job": job,
                "min_confidence": Decimal(str(config.creatives_min_confidence)),
                "labels": {name: Decimal(f"{confidence:.1f}") for name, confidence in sorted(flagged.items())},
            }
        },
    )
    return {"status": "SUCCEEDED", "state": state.value}


def decide(decision: Decision, execution: str) -> dict[str, str]:
    """Apply a person's decision; returns what the state machine does next (:data:`NEXT`)."""
    record = _item(decision.crid)
    expected, wanted = DECIDED[decision.action]
    if (current := state_of(record)) is not expected:
        raise TransitionError(f"{decision.action} applies to a creative {expected}, {decision.crid} is {current}")
    _move(record, wanted, by=decision.by, note=decision.note, execution=execution)
    return {"crid": decision.crid, "state": wanted.value, "next": NEXT[decision.action]}


def ladder() -> Ladder:
    return Ladder.model_validate_json(settings().creatives_ladder)


def conditioning(crid: str, execution: str) -> dict[str, Any]:
    """CONDITIONING, and the ingest request that encodes the creative to the ladder (the pipeline's ``ad`` kind)."""
    record = _item(crid)
    target = ladder()
    request = IngestRequest(
        source_uri=str(record["object"]),
        kind=AssetKind.AD,
        asset_id=f"{crid}-{digest(execution)}",  # an attempt of its own: an encode is never overwritten
        ladder_version=target.version,
        correlation_id=f"{execution[:72]}-ingest",
        metadata={"crid": crid, "adomain": str(record["adomain"])},
    )
    _move(record, State.CONDITIONING, by="readiness", execution=execution, values={"ladder_version": target.version})
    return request.model_dump(mode="json", exclude_none=True)


def _read(bucket: str, key: str) -> str | None:
    s3 = aws.client("s3")
    try:
        body: bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    return body.decode()


def conform(crid: str, asset: Mapping[str, Any]) -> dict[str, str]:
    """Check the encode's CDN copy against the ladder: CONDITIONED, or REJECTED with the problems."""
    config, target = settings(), ladder()
    record = _item(crid)
    creative = asset["creative"]
    prefix = str(creative["ad_prefix"])
    main = _read(config.media_bucket, prefix + PurePosixPath(urlsplit(creative["hls_uri"]).path).name) or ""
    media = {
        variant.uri: text
        for variant in parse_multivariant(main)
        if (text := _read(config.media_bucket, prefix + variant.uri)) is not None
    }
    result = check(main, media, target)
    top = max(target.video, key=lambda rendition: rendition.width * rendition.height)
    state = State.CONDITIONED if result.conformant else State.REJECTED
    _move(
        record,
        state,
        by="conformance",
        note="; ".join(result.problems),
        values={
            "conformance": result.model_dump(mode="json"),
            "creative_id": creative["creative_id"],
            "hls_uri": creative["hls_uri"],
            "duration_ms": result.duration_ms or int(asset["duration_ms"]),
            "width": top.width,
            "height": top.height,
            "asset_id": asset["asset_id"],
        },
    )
    return {"crid": crid, "state": state.value}


def ready_creatives(ladder_version: str) -> ReadyCreatives:
    """The READY creatives on ``ladder_version``, oldest first (the registry's state index)."""
    table = aws.table(settings().creatives_table)
    items: list[dict[str, Any]] = []
    request: dict[str, Any] = {"IndexName": STATE_INDEX, "KeyConditionExpression": Key("state").eq(State.READY.value)}
    while True:
        page = table.query(**request)
        items += page.get("Items", [])
        if not (start := page.get("LastEvaluatedKey")):
            break
        request["ExclusiveStartKey"] = start
    ready = sorted(
        (i for i in items if i.get("ladder_version") == ladder_version),
        key=lambda i: (str(i.get("ready_at", "")), str(i["crid"])),
    )
    return ReadyCreatives(
        ladder_version=ladder_version,
        creatives=tuple(
            RegisteredCreative(
                crid=i["crid"],
                creative_id=i["creative_id"],
                hls_uri=i["hls_uri"],
                duration_ms=int(i["duration_ms"]),
                width=int(i["width"]),
                height=int(i["height"]),
                advertiser=i["advertiser"],
                adomain=i["adomain"],
            )
            for i in ready
        ),
    )


def publish(crid: str, execution: str) -> dict[str, Any]:
    """READY (from CONDITIONED), then the READY creatives to the PDS config bucket and a run of the publisher."""
    config = settings()
    record = _item(crid)
    if state_of(record) is State.CONDITIONED:
        _move(record, State.READY, by="readiness", execution=execution, values={"ready_at": now_iso()})
    document = ready_creatives(ladder().version)
    aws.client("s3").put_object(
        Bucket=config.pds_config_bucket,
        Key=config.pds_creatives_key,
        Body=document.model_dump_json(indent=1).encode(),
        ContentType="application/json",
    )
    aws.client("lambda").invoke(FunctionName=config.pds_publisher_function, InvocationType="Event", Payload=b"{}")
    ready = [creative.crid for creative in document.creatives]
    logger.info("READY creatives published", extra={"ready": ready})
    return {"crid": crid, "ready": ready}


def failed(request: Mapping[str, Any], execution: str, error: Mapping[str, Any]) -> dict[str, str]:
    """FAILED, where the creative's state allows it; returns the error for the state machine's Fail state."""
    name, cause = str(error.get("Error") or "ReadinessFailed"), str(error.get("Cause") or "")
    crid = request.get("crid")
    if isinstance(crid, str) and (record := item(crid)) is not None:
        current = state_of(record)
        if State.FAILED in TRANSITIONS[current]:
            _move(record, State.FAILED, by="readiness", note=f"{name}: {cause}", execution=execution)
    logger.error("readiness failed", extra={"crid": crid, "error": name, "cause": cause[:1000]})
    return {"error": name, "cause": cause[:4000]}


type Step = Callable[[Mapping[str, Any], str], Mapping[str, Any]]

STEPS: Final[Mapping[str, Step]] = {
    "received": lambda event, execution: received(Registration.model_validate(event["input"]), execution),
    "fetch": lambda event, _: fetch(event["crid"]),
    "moderate": lambda event, _: moderate(event["crid"], event.get("job")),
    "decide": lambda event, execution: decide(Decision.model_validate(event["input"]), execution),
    "conditioning": lambda event, execution: conditioning(event["crid"], execution),
    "conform": lambda event, _: conform(event["crid"], event["asset"]),
    "publish": lambda event, execution: publish(event["crid"], execution),
    "failed": lambda event, execution: failed(event.get("input") or {}, execution, event.get("error") or {}),
}
"""The state machine's steps (module docstring), each given the event and the execution's name."""


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Dispatch on ``event["step"]`` (:data:`STEPS`)."""
    settings().require(*REQUIRED)
    if (step := STEPS.get(str(event.get("step")))) is None:
        raise ValueError(f"unknown step {event.get('step')!r}")
    return dict(step(event, str(event.get("execution", ""))))
