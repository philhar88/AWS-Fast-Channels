import json
from typing import Any

import pytest
from botocore.stub import ANY

from fast_channels.core.ingest import asset_id_for
from fast_channels.core.ladder import Ladder
from fast_channels.functions.ingest import publish

STANDARD = Ladder.builtin("standard")
ASSET = {
    "asset_id": "house-ad-1-1a2b3c",
    "kind": "ad",
    "ladder_version": STANDARD.version,
    "correlation_id": "kit-1-house-ad-1",
    "label": "house-ad-1",
}
CREATIVE = {
    "creative_id": "c-1",
    "ad_prefix": "ads/x/c-1/",
    "hls_uri": "https://h/main.m3u8",
}


def expect_update(stubs: Any, asset_id: str) -> None:
    stubs["dynamodb"].add_response(
        "update_item",
        {},
        {
            "TableName": "library",
            "Key": {"pk": f"ASSET#{asset_id}", "sk": f"LADDER#{STANDARD.version}"},
            "UpdateExpression": ANY,
            "ExpressionAttributeNames": ANY,
            "ExpressionAttributeValues": ANY,
            "ConditionExpression": ANY,
        },
    )


def test_ready_ad(stubs: Any, expect_event: Any, lambda_context: Any) -> None:
    expect_update(stubs, "house-ad-1-1a2b3c")
    expect_event("Asset Ready", asset_id="house-ad-1-1a2b3c", vod_source=None, duration_ms=14014)

    result = publish.handler(
        {"status": "READY", "asset": ASSET, "encoded": {"duration_ms": 14014, "creative": CREATIVE}, "registered": []},
        lambda_context,
    )

    assert result == {
        "asset_id": "house-ad-1-1a2b3c",
        "kind": "ad",
        "ladder_version": STANDARD.version,
        "label": "house-ad-1",
        "correlation_id": "kit-1-house-ad-1",
        "duration_ms": 14014,
        "vod_source": None,
        "creative": CREATIVE,
    }


def test_ready_content_records_its_vod_source(stubs: Any, expect_event: Any, lambda_context: Any) -> None:
    expect_update(stubs, "bbb-1a2b3c")
    expect_event("Asset Ready", vod_source="bbb-src")
    registered = [
        {"vod_source": "bbb-src", "vod_source_arn": "arn", "source_location": "o", "packages": {}, "alerts": []}
    ]

    result = publish.handler(
        {
            "status": "READY",
            "asset": {**ASSET, "asset_id": "bbb-1a2b3c", "kind": "content", "label": None},
            "encoded": {"duration_ms": 596_000},
            "registered": registered,
        },
        lambda_context,
    )

    assert result["vod_source"] == "bbb-src"


def test_failed_lambda_step(stubs: Any, expect_event: Any, lambda_context: Any) -> None:
    request = {"source_uri": "s3://ingest/movie.mp4", "ladder_version": STANDARD.version}
    asset_id = asset_id_for("s3://ingest/movie.mp4")
    expect_update(stubs, asset_id)
    expect_event("Asset Failed", asset_id=asset_id, error="IngestRefusedError", cause="HDR source")
    cause = json.dumps({"errorMessage": "HDR source", "errorType": "IngestRefusedError", "stackTrace": []})

    result = publish.handler(
        {
            "status": "FAILED",
            "request": request,
            "execution": "run-1",
            "error": {"Error": "IngestRefusedError", "Cause": cause},
        },
        lambda_context,
    )

    assert result == {"error": "IngestRefusedError", "cause": "HDR source"}


def test_a_failure_still_reaches_the_bus_when_another_execution_owns_the_item(
    stubs: Any, expect_event: Any, lambda_context: Any
) -> None:
    # Prepare refused to encode a READY asset again: its item stays as it is, the refusal is still announced.
    request = {"source_uri": "s3://ingest/movie.mp4", "ladder_version": STANDARD.version}
    asset_id = asset_id_for("s3://ingest/movie.mp4")
    stubs["dynamodb"].add_client_error("update_item", "ConditionalCheckFailedException")
    expect_event("Asset Failed", asset_id=asset_id, error="IngestRefusedError", correlation_id="run-3")
    cause = json.dumps({"errorMessage": f"{asset_id} is already READY", "errorType": "IngestRefusedError"})

    result = publish.handler(
        {
            "status": "FAILED",
            "request": request,
            "execution": "run-3",
            "error": {"Error": "IngestRefusedError", "Cause": cause},
        },
        lambda_context,
    )

    assert result == {"error": "IngestRefusedError", "cause": f"{asset_id} is already READY"}


def test_failed_invalid_request_only_publishes(expect_event: Any, lambda_context: Any) -> None:
    expect_event("Asset Failed", asset_id="unknown", ladder_version="", correlation_id="run-2")

    publish.handler(
        {"status": "FAILED", "request": {"kind": "nope"}, "execution": "run-2", "error": {"Error": "States.Runtime"}},
        lambda_context,
    )


@pytest.mark.parametrize(
    ("cause", "summary"),
    [
        (None, ""),
        ("plain text", "plain text"),
        (json.dumps({"errorMessage": "boom"}), "boom"),
        (json.dumps({"Job": {"ErrorCode": 1030, "ErrorMessage": "bad input"}}), "1030: bad input"),
        (
            json.dumps({"Error": "States.TaskFailed", "Cause": json.dumps({"errorMessage": "child"})}),
            "States.TaskFailed: child",
        ),
        (json.dumps([1, 2]), "[1, 2]"),
        ("x" * 2000, "x" * publish.MAX_CAUSE),
    ],
)
def test_summarize_cause(cause: Any, summary: str) -> None:
    assert publish.summarize_cause(cause) == summary


def test_unknown_status(lambda_context: Any) -> None:
    with pytest.raises(ValueError, match="unknown status"):
        publish.handler({"status": "MAYBE"}, lambda_context)
