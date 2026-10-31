"""The account's as-run router: subscription batches become default-bus events, ten per call, failures retried."""

import base64
import gzip
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from fast_channels.functions import aws
from fast_channels.functions.ops import as_run_router as router


@dataclass
class Context:
    function_name: str = "as-run-router"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:as-run-router"
    aws_request_id: str = "request-1"


@dataclass
class FakeEvents:
    """``put_events``, refusing the first ``refuse`` entries of each of the first ``failing_calls`` calls."""

    refuse: int = 0
    failing_calls: int = 0
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    def put_events(self, *, Entries: list[dict[str, str]]) -> dict[str, Any]:  # noqa: N803
        self.calls.append(Entries)
        refused = self.refuse if len(self.calls) <= self.failing_calls else 0
        results = [{"ErrorCode": "Throttling"} if i < refused else {"EventId": f"e{i}"} for i in range(len(Entries))]
        return {"FailedEntryCount": min(refused, len(Entries)), "Entries": results}


@pytest.fixture
def events(monkeypatch: pytest.MonkeyPatch) -> FakeEvents:
    fake = FakeEvents()
    monkeypatch.setattr(aws, "client", lambda service: fake)
    return fake


def as_run(i: int, event_type: str = "AdBreakStart", channel: str = "fastm-demo") -> str:
    return json.dumps(
        {
            "channelName": channel,
            "eventType": event_type,
            "programName": f"p{i}",
            "sourceGroup": "hls",
            "timeStamp": f"2026-10-29T03:00:{i:02d}.000Z",
        }
    )


def subscription(*messages: str) -> dict[str, Any]:
    data = {
        "messageType": "DATA_MESSAGE",
        "owner": "123456789012",
        "logGroup": "MediaTailor/Channel/AsRunLog",
        "logStream": "fastm-demo-hls",
        "subscriptionFilters": ["fast-channels-as-run-router"],
        "logEvents": [{"id": f"id{i}", "timestamp": 1793250000000 + i, "message": m} for i, m in enumerate(messages)],
    }
    return {"awslogs": {"data": base64.b64encode(gzip.compress(json.dumps(data).encode())).decode()}}


def test_every_as_run_event_goes_to_the_default_bus_with_its_id(events: FakeEvents) -> None:
    result = router.handler(subscription(*(as_run(i) for i in range(23)), "not json", "[1]"), Context())

    assert result == {"routed": 23}
    assert [len(call) for call in events.calls] == [10, 10, 3]
    first = events.calls[0][0]
    assert (first["Source"], first["DetailType"]) == ("fast-channels.as-run", "AdBreakStart")
    detail = json.loads(first["Detail"])
    assert detail["channelName"] == "fastm-demo"
    assert detail["as_run_id"] == "fastm-demo-hls/id0"
    assert "EventBusName" not in first  # the account's default bus


def test_the_router_logs_the_ids_it_puts(events: FakeEvents, caplog: pytest.LogCaptureFixture) -> None:
    router.handler(subscription(as_run(0), as_run(1)), Context())

    [record] = [r for r in caplog.records if r.getMessage() == "as-run events put"]
    line = json.loads(router.logger.registered_formatter.format(record))  # as it reaches CloudWatch
    assert line["as_run_ids"] == ["fastm-demo-hls/id0", "fastm-demo-hls/id1"]
    assert line["correlation_id"] == "fastm-demo-hls/id0"


def test_refused_entries_are_put_again(events: FakeEvents) -> None:
    events.refuse, events.failing_calls = 2, 1

    router.handler(subscription(*(as_run(i) for i in range(5))), Context())

    assert [len(call) for call in events.calls] == [5, 2]
    assert events.calls[1] == events.calls[0][:2]


def test_a_batch_still_refused_raises_so_lambda_retries_it(events: FakeEvents) -> None:
    events.refuse, events.failing_calls = 1, 99

    with pytest.raises(router.RouteError, match="refused 1 as-run entries after 3 attempts"):
        router.handler(subscription(as_run(0)), Context())
    assert len(events.calls) == 3


def test_an_empty_batch_puts_nothing(events: FakeEvents) -> None:
    assert router.handler(subscription("{}"), Context()) == {"routed": 0}
    assert events.calls == []
