import base64
import gzip
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from botocore.stub import ANY, Stubber

from fast_channels.functions.as_run_reconciler import handler

if TYPE_CHECKING:
    import pytest

type Stub = Callable[[str], Stubber]

PROGRAM = "20261016T120000-bbb-1a2b3c"
KEY = {"pk": "CHANNEL#fast-demo", "sk": "PROGRAM#1792152000000"}


def as_run(event_type: str, *, source_group: str = "hls", program: str = PROGRAM) -> str:
    return json.dumps(
        {
            "channelName": "fast-demo",
            "eventType": event_type,
            "programName": program,
            "sourceGroup": source_group,
            "sourceLocationName": "fast-media",
            "timeStamp": "2026-10-16T12:00:03.170Z",
            "vodSourceName": "bbb",
        }
    )


def subscription(*messages: str) -> dict[str, Any]:
    """A CloudWatch Logs subscription event (base64 of gzipped JSON)."""
    data = {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "MediaTailor/Channel/AsRunLog",
        "logStream": "fast-demo-hls",
        "subscriptionFilters": ["fast-as-run"],
        "logEvents": [{"id": str(i), "timestamp": 1792152003170, "message": m} for i, m in enumerate(messages)],
    }
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()}}


def lookup(stub: Stub, typed: Callable[..., dict[str, Any]], items: list[dict[str, Any]]) -> None:
    stub("dynamodb").add_response(
        "query",
        {"Items": [typed(item) for item in items]},
        {"TableName": "library", "IndexName": "GSI1", "KeyConditionExpression": ANY},
    )


def aired(**extra: Any) -> dict[str, Any]:
    return {
        "TableName": "library",
        "Key": KEY,
        "UpdateExpression": "SET #status = :aired, aired_at = :at, updated_at = :now",
        "ConditionExpression": "attribute_exists(pk) AND #status <> :aired",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":aired": "AIRED", ":at": "2026-10-16T12:00:03.170Z", ":now": ANY},
    } | extra


def test_the_first_start_event_marks_the_program_aired_once(
    stub: Stub,
    typed: Callable[..., dict[str, Any]],
    client_error: Callable[..., dict[str, Any]],
    context: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = {**KEY, "name": PROGRAM, "status": "PUBLISHED", "correlation_id": "session-1"}
    lookup(stub, typed, [item])
    stub("dynamodb").add_response("update_item", {}, aired())
    lookup(stub, typed, [{**item, "status": "AIRED"}])
    stub("dynamodb").add_client_error("update_item", **client_error("ConditionalCheckFailedException"))

    handler(
        subscription(
            as_run("VodProgramStart"),
            as_run("AdBreakStart"),
            "not json",
            as_run("VodProgramStart", source_group="dash-avc"),
        ),
        context,
    )

    metrics = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]
    assert [m["ProgramsAired"] for m in metrics] == [[1.0]]


def test_programs_outside_the_library_are_ignored(
    stub: Stub, typed: Callable[..., dict[str, Any]], context: Any
) -> None:
    lookup(stub, typed, [])

    handler(subscription(as_run("VodProgramStart", program="manual")), context)
