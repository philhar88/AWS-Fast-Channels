"""The reconciler's break marking (ops module): ``AdBreakStart`` events mark the scheduled break aired, the sweep marks
the breaks that never aired. Replays a live run (2026-10-27): 4 programs, 24 planned breaks, 20 aired before the
channel stopped."""

import base64
import gzip
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from fast_channels.core.timeline import scte_event_id
from fast_channels.functions import as_run_reconciler as reconciler
from fast_channels.functions.settings import settings

type Stub = Callable[[str], Stubber]

RUN: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "core" / "tests" / "data" / "as-run-2026-10-27.json").read_text()
)
KEY = {"pk": "CHANNEL#fastv3-exp-p0d-ch", "sk": "PROGRAM#1793135280000"}


def ms(iso: str) -> int:
    return round(datetime.fromisoformat(iso).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _breaks_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AS_RUN_BREAKS", "true")
    settings.cache_clear()


def subscription(*events: dict[str, Any]) -> dict[str, Any]:
    data = {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "MediaTailor/Channel/AsRunLog",
        "logStream": "fastv3-exp-p0d-ch-hls",
        "subscriptionFilters": ["fast-as-run"],
        "logEvents": [
            {"id": str(i), "timestamp": ms(e["timeStamp"]), "message": json.dumps(e)} for i, e in enumerate(events)
        ],
    }
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()}}


def program_item(index: int, **extra: Any) -> dict[str, Any]:
    program = RUN["programs"][index]
    return {
        "pk": f"CHANNEL#{RUN['channel']}",
        "sk": f"PROGRAM#{program['start_ms']:013d}",
        "name": program["name"],
        "status": "PUBLISHED",
        "start_ms": program["start_ms"],
        "breaks": json.loads(json.dumps(program["breaks"])),  # a copy: the tests mark breaks
        "correlation_id": "run-1",
        **extra,
    }


def test_a_break_start_marks_its_break_aired_once(
    stub: Stub,
    typed: Callable[..., dict[str, Any]],
    client_error: Callable[..., dict[str, Any]],
    context: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    [first] = [e for e in RUN["as_run"] if e["eventType"] == "AdBreakStart"][:1]
    item = program_item(0, aired_at="2026-10-27T21:08:00.959Z")
    expected = {
        "TableName": "library",
        "Key": KEY,
        "UpdateExpression": "SET #breaks[0].#status = :status, #breaks[0].aired_at = :at",
        "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(#breaks[0].#status)",
        "ExpressionAttributeNames": {"#breaks": "breaks", "#status": "status"},
        "ExpressionAttributeValues": {":status": "AIRED", ":at": first["timeStamp"]},
    }
    for response in ({}, None):  # the second (another output's event) is already decided
        stub("dynamodb").add_response(
            "query",
            {"Items": [typed(item)]},
            {"TableName": "library", "IndexName": "GSI1", "KeyConditionExpression": ANY},
        )
        if response is None:
            stub("dynamodb").add_client_error("update_item", **client_error("ConditionalCheckFailedException"))
        else:
            stub("dynamodb").add_response("update_item", response, expected)

    reconciler.handler(subscription(first, {**first, "sourceGroup": "dash"}), context)

    metrics = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"_aws"' in line]
    assert [m["BreaksAired"] for m in metrics] == [[1.0]]


def test_break_starts_are_ignored_without_the_ops_module(
    monkeypatch: pytest.MonkeyPatch, context: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AS_RUN_BREAKS")
    settings.cache_clear()
    [first] = [e for e in RUN["as_run"] if e["eventType"] == "AdBreakStart"][:1]

    reconciler.handler(subscription(first), context)  # no AWS call: no stub queued

    assert "BreaksAired" not in capsys.readouterr().out


class Library:
    """The PROGRAM items of the run, with the reconciler's two writes applied in memory."""

    def __init__(self) -> None:
        self.items = {p["name"]: program_item(i) for i, p in enumerate(RUN["programs"])}

    def program(self, start: reconciler.AsRunEvent) -> dict[str, Any] | None:
        item = self.items.get(str(start.program))
        return None if item is None else dict(item)

    def decide(
        self, item: dict[str, Any], index: int, status: reconciler.BreakStatus, *, aired_at: str | None = None
    ) -> bool:
        stored = self.items[item["name"]]["breaks"][index]
        if "status" in stored:
            return False
        stored["status"] = status.value
        if aired_at is not None:
            stored["aired_at"] = aired_at
        return True


def test_the_run_reconciles_20_aired_and_4_unaired_breaks(monkeypatch: pytest.MonkeyPatch, context: Any) -> None:
    library = Library()
    monkeypatch.setattr(reconciler, "_program", library.program)
    monkeypatch.setattr(reconciler, "_decide", library.decide)
    monkeypatch.setattr(
        reconciler, "mark_aired", lambda start: library.items[start.program].update(aired_at=start.timestamp) or True
    )
    monkeypatch.setattr(reconciler, "_recent_programs", lambda since: iter(library.items.values()))

    reconciler.handler(subscription(*RUN["as_run"]), context)
    aired = {
        (name, i): b
        for name, item in library.items.items()
        for i, b in enumerate(item["breaks"])
        if b.get("status") == "AIRED"
    }
    assert len(aired) == 20
    for (name, i), stored in aired.items():
        assert stored["event_id"] == scte_event_id(name, i)
    monkeypatch.setattr(reconciler, "_now_ms", lambda: ms(RUN["stopped_at"]) + 3600_000)

    assert reconciler.sweep({}, context) == {"unaired": 4}
    last = library.items[RUN["programs"][3]["name"]]["breaks"]
    assert [b["status"] for b in last] == ["AIRED", "AIRED", "UNAIRED", "UNAIRED", "UNAIRED", "UNAIRED"]
    assert reconciler.sweep({}, context) == {"unaired": 0}  # decided breaks stay decided


def test_a_skipped_programs_breaks_never_air(monkeypatch: pytest.MonkeyPatch, context: Any) -> None:
    library = Library()
    skipped = library.items[RUN["programs"][1]["name"]]
    skipped["status"] = "SKIPPED"
    monkeypatch.setattr(reconciler, "_decide", library.decide)
    monkeypatch.setattr(reconciler, "_recent_programs", lambda since: iter([skipped]))
    monkeypatch.setattr(reconciler, "_now_ms", lambda: RUN["programs"][1]["start_ms"])  # even before its time

    assert reconciler.sweep({}, context) == {"unaired": len(skipped["breaks"])}


def test_a_break_start_at_no_breaks_time_is_ignored(monkeypatch: pytest.MonkeyPatch, context: Any) -> None:
    library = Library()
    monkeypatch.setattr(reconciler, "_program", library.program)
    monkeypatch.setattr(reconciler, "_decide", library.decide)
    [first] = [e for e in RUN["as_run"] if e["eventType"] == "AdBreakStart"][:1]

    reconciler.handler(subscription({**first, "timeStamp": "2026-10-27T21:09:00.000Z"}), context)
    reconciler.handler(subscription({**first, "programName": "manual"}), context)

    assert not [b for item in library.items.values() for b in item["breaks"] if "status" in b]


def test_the_sweep_reads_every_channels_recent_programs(
    stub: Stub, typed: Callable[..., dict[str, Any]], context: Any
) -> None:
    channel = {"pk": "CHANNEL#fast-demo", "sk": "META", "gsi1pk": "CHANNELS", "gsi1sk": "iac#fast-demo"}
    stub("dynamodb").add_response(
        "query",
        {"Items": [typed(channel)], "LastEvaluatedKey": typed({"pk": "CHANNEL#fast-demo", "sk": "META"})},
        {"TableName": "library", "IndexName": "GSI1", "KeyConditionExpression": ANY},
    )
    # Lazily: the first page of channels, that channel's programs, then the next page of channels.
    stub("dynamodb").add_response("query", {"Items": []}, {"TableName": "library", "KeyConditionExpression": ANY})
    stub("dynamodb").add_response(
        "query",
        {"Items": []},
        {"TableName": "library", "IndexName": "GSI1", "KeyConditionExpression": ANY, "ExclusiveStartKey": ANY},
    )

    assert reconciler.sweep({}, context) == {"unaired": 0}


def test_other_write_errors_fail_the_batch(
    stub: Stub, typed: Callable[..., dict[str, Any]], client_error: Callable[..., dict[str, Any]], context: Any
) -> None:
    [first] = [e for e in RUN["as_run"] if e["eventType"] == "AdBreakStart"][:1]
    stub("dynamodb").add_response(
        "query",
        {"Items": [typed(program_item(0))]},
        {"TableName": "library", "IndexName": "GSI1", "KeyConditionExpression": ANY},
    )
    stub("dynamodb").add_client_error("update_item", **client_error("ProvisionedThroughputExceededException"))

    with pytest.raises(ClientError, match="ProvisionedThroughputExceeded"):
        reconciler.handler(subscription(first), context)
