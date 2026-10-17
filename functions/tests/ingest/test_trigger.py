import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.response import StreamingBody

from fast_channels.core.ingest import IngestRequest, asset_id_for
from fast_channels.core.ladder import Ladder
from fast_channels.core.naming import digest
from fast_channels.functions.ingest import trigger

STANDARD = Ladder.builtin("standard")
MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:fast-ingest"
KEY = "movies/big_buck_bunny.mp4"
SOURCE = f"s3://ingest/{KEY}"
STARTED = {"executionArn": f"{MACHINE.replace('stateMachine', 'execution')}:x", "startDate": datetime.now(UTC)}


def upload(key: str = KEY) -> dict[str, Any]:
    return {
        "id": "event-1",
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {"bucket": {"name": "ingest"}, "object": {"key": key, "size": 10, "sequencer": "0066A1B2C3"}},
    }


def requested(detail: dict[str, Any]) -> dict[str, Any]:
    return {"id": "event-2", "source": "fast-channels.fast", "detail-type": "Ingest Requested", "detail": detail}


def body(document: Any) -> dict[str, Any]:
    data = json.dumps(document).encode()
    return {"Body": StreamingBody(io.BytesIO(data), len(data))}


def expect_sidecar(stubs: Any, sidecar: dict[str, Any] | None) -> None:
    if sidecar is None:
        stubs["s3"].add_client_error("get_object", "NoSuchKey", http_status_code=404)
    else:
        stubs["s3"].add_response("get_object", body(sidecar), {"Bucket": "ingest", "Key": f"{KEY}.json"})


def expect_start(stubs: Any, request: IngestRequest) -> None:
    stubs["stepfunctions"].add_response(
        "start_execution",
        STARTED,
        {
            "stateMachineArn": MACHINE,
            "name": request.correlation_id,
            "input": request.model_dump_json(exclude_none=True),
        },
    )


def default_name(seed: str) -> str:
    return f"{asset_id_for(SOURCE)}-{digest(seed, 12)}"


def test_upload_with_sidecar_and_tag(stubs: Any, lambda_context: Any) -> None:
    expect_sidecar(stubs, {"content_id": "bbb", "metadata": {"title": "Big Buck Bunny"}})
    stubs["s3"].add_response(
        "get_object_tagging",
        {"TagSet": [{"Key": "AdOffsets", "Value": "30000 90000,120000"}]},
        {"Bucket": "ingest", "Key": KEY},
    )
    name = default_name(f"ingest/{KEY}#0066A1B2C3")
    expect_start(
        stubs,
        IngestRequest(
            source_uri=SOURCE,
            ladder_version=STANDARD.version,
            content_id="bbb",
            metadata={"title": "Big Buck Bunny"},
            cue_points_ms=[30000, 90000, 120000],
            correlation_id=name,
        ),
    )

    assert trigger.handler(upload(), lambda_context)["status"] == "STARTED"


def test_sidecar_cue_points_win_over_the_tag(stubs: Any, lambda_context: Any) -> None:
    sidecar = {"cue_points_ms": [60000], "correlation_id": "my-ingest-1", "ladder_version": STANDARD.version}
    expect_sidecar(stubs, sidecar)
    expect_start(stubs, IngestRequest(source_uri=SOURCE, **sidecar))

    assert trigger.handler(upload(), lambda_context) == {
        "status": "STARTED",
        "correlation_id": "my-ingest-1",
        "execution_arn": STARTED["executionArn"],
    }


def test_upload_without_sidecar_or_tags(stubs: Any, lambda_context: Any) -> None:
    expect_sidecar(stubs, None)
    stubs["s3"].add_response("get_object_tagging", {"TagSet": []}, {"Bucket": "ingest", "Key": KEY})
    name = default_name(f"ingest/{KEY}#0066A1B2C3")
    expect_start(stubs, IngestRequest(source_uri=SOURCE, ladder_version=STANDARD.version, correlation_id=name))

    trigger.handler(upload(), lambda_context)


def test_ingest_requested_event(stubs: Any, lambda_context: Any) -> None:
    request = IngestRequest(source_uri=SOURCE, ladder_version=STANDARD.version, kind="ad", correlation_id="session-1")
    expect_start(stubs, request)

    trigger.handler(requested(request.model_dump(mode="json", exclude_none=True)), lambda_context)


def test_ingest_requested_without_correlation_id_is_named_after_the_event(stubs: Any, lambda_context: Any) -> None:
    detail = {"source_uri": SOURCE, "ladder_version": STANDARD.version}
    expect_start(stubs, IngestRequest(**detail, correlation_id=default_name("event-2")))

    trigger.handler(requested(detail), lambda_context)


def described(name: str, started: datetime, request: dict[str, Any], status: str = "RUNNING") -> dict[str, Any]:
    return {
        "executionArn": f"{MACHINE.replace(':stateMachine:', ':execution:')}:{name}",
        "stateMachineArn": MACHINE,
        "status": status,
        "startDate": started,
        "input": IngestRequest.model_validate(request).model_dump_json(exclude_none=True),
    }


REQUEST = {"source_uri": SOURCE, "ladder_version": STANDARD.version, "correlation_id": "c-1"}
EXECUTION = {"executionArn": f"{MACHINE.replace(':stateMachine:', ':execution:')}:c-1"}


def test_redelivered_event_is_ignored(stubs: Any, lambda_context: Any) -> None:
    stubs["stepfunctions"].add_client_error("start_execution", "ExecutionAlreadyExists")
    started = datetime(2026, 10, 16, 12, 0, 0, 700_000, tzinfo=UTC)  # this event (12:00:00.4) started it
    stubs["stepfunctions"].add_response("describe_execution", described("c-1", started, REQUEST), EXECUTION)

    result = trigger.handler({**requested(REQUEST), "time": "2026-10-16T12:00:00Z"}, lambda_context)

    assert result == {"status": "DUPLICATE", "correlation_id": "c-1"}


@pytest.mark.parametrize("status", ["RUNNING", "SUCCEEDED"])
def test_a_new_request_reusing_a_correlation_id_is_reported(
    stubs: Any, expect_event: Any, lambda_context: Any, status: str
) -> None:
    # A CMS re-sends Ingest Requested with the same id after fixing its source: the execution predates the event.
    stubs["stepfunctions"].add_client_error("start_execution", "ExecutionAlreadyExists")
    started = datetime(2026, 10, 16, 11, 0, tzinfo=UTC)
    stubs["stepfunctions"].add_response("describe_execution", described("c-1", started, REQUEST, status), EXECUTION)
    expect_event(
        "Asset Failed",
        asset_id=asset_id_for(SOURCE),
        error="DuplicateCorrelationId",
        correlation_id="c-1",
        cause=lambda cause: (
            f"already exists ({status}, started 2026-10-16T11:00:00Z)" in cause and "new correlation_id" in cause
        ),
    )

    result = trigger.handler({**requested(REQUEST), "time": "2026-10-16T12:00:00Z"}, lambda_context)

    assert result == {"status": "REJECTED", "correlation_id": "c-1"}


def test_without_an_event_time_only_an_identical_request_is_a_redelivery() -> None:
    request = IngestRequest.model_validate(REQUEST)
    execution = described("c-1", datetime(2026, 10, 16, tzinfo=UTC), REQUEST)
    other = request.model_copy(update={"cue_points_ms": [60_000]})

    assert trigger.redelivered({}, execution, request)
    assert not trigger.redelivered({}, execution, other)


def test_an_execution_it_cannot_describe_is_taken_for_a_redelivery(stubs: Any, lambda_context: Any) -> None:
    stubs["stepfunctions"].add_client_error("start_execution", "ExecutionAlreadyExists")
    stubs["stepfunctions"].add_client_error("describe_execution", "AccessDeniedException", http_status_code=400)

    assert trigger.handler(requested(REQUEST), lambda_context)["status"] == "DUPLICATE"


def test_invalid_sidecar_is_rejected_as_asset_failed(stubs: Any, expect_event: Any, lambda_context: Any) -> None:
    expect_sidecar(stubs, {"source_uri": "s3://elsewhere/x.mp4"})
    expect_event(
        "Asset Failed",
        asset_id=asset_id_for(SOURCE),
        error="InvalidIngestRequest",
        cause=lambda cause: "must not set source_uri" in cause,
    )

    assert trigger.handler(upload(), lambda_context)["status"] == "REJECTED"


def test_unparsable_tag_is_rejected(stubs: Any, expect_event: Any, lambda_context: Any) -> None:
    expect_sidecar(stubs, None)
    stubs["s3"].add_response("get_object_tagging", {"TagSet": [{"Key": "ad_offsets", "Value": "soon"}]}, None)
    expect_event(
        "Asset Failed",
        asset_id=asset_id_for(SOURCE),
        error="InvalidIngestRequest",
        cause=lambda cause: "not a list of milliseconds" in cause,
    )

    assert trigger.handler(upload(), lambda_context)["status"] == "REJECTED"


def test_invalid_request_event_is_rejected(expect_event: Any, lambda_context: Any) -> None:
    expect_event(
        "Asset Failed", asset_id="unknown", error="InvalidIngestRequest", cause=lambda cause: "ladder_version" in cause
    )

    assert trigger.handler(requested({"kind": "slate"}), lambda_context)["status"] == "REJECTED"


@pytest.mark.parametrize(("sidecar", "cause"), [(b"{not json", "not valid JSON"), (b"[1]", "must be a JSON object")])
def test_malformed_sidecar_is_rejected(
    stubs: Any, expect_event: Any, lambda_context: Any, sidecar: bytes, cause: str
) -> None:
    stubs["s3"].add_response("get_object", {"Body": StreamingBody(io.BytesIO(sidecar), len(sidecar))}, None)
    expect_event("Asset Failed", cause=lambda text: cause in text)

    assert trigger.handler(upload(), lambda_context)["status"] == "REJECTED"


def test_unsupported_event_is_rejected(expect_event: Any, lambda_context: Any) -> None:
    expect_event("Asset Failed", asset_id="unknown", cause=lambda text: "unsupported event" in text)

    event = {
        "id": "e",
        "source": "aws.s3",
        "detail-type": "Object Deleted",
        "detail": {"bucket": {"name": "b"}, "object": {"key": "k.mp4"}},
    }
    assert trigger.handler(event | {"source": "other"}, lambda_context)["status"] == "REJECTED"
