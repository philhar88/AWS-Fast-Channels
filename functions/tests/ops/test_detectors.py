"""The ``ListAlerts`` poller and the zero-ad-sessions detector, with in-memory stand-ins for their AWS clients."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fast_channels.core.events import ChannelAlert
from fast_channels.functions import aws
from fast_channels.functions.ops import list_alerts, zero_ads_sessions
from fast_channels.functions.settings import settings

SCHEDULED = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
CHANNEL = {
    "ChannelName": "fastm-demo",
    "Arn": "arn:aws:mediatailor:us-east-1:123456789012:channel/fastm-demo",
    "ChannelState": "RUNNING",
    "Tags": {"fast:app": "fastm"},
}


@dataclass
class Context:
    function_name: str = "test"
    memory_limit_in_mb: int = 256
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:test"
    aws_request_id: str = "request-1"


class Pages:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages

    def paginate(self, **_: Any) -> list[dict[str, Any]]:
        return self.pages


@dataclass
class FakeClients:
    """MediaTailor (paginated list_channels and list_alerts), SSM, EventBridge and CloudWatch Logs Insights."""

    channels: list[dict[str, Any]] = field(default_factory=lambda: [CHANNEL])
    alerts: list[dict[str, Any]] = field(default_factory=list)
    running: str = "1"
    put: list[list[dict[str, str]]] = field(default_factory=list)
    refused: int = 0
    query_status: list[str] = field(default_factory=lambda: ["Running", "Complete"])
    results: list[list[dict[str, str]]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)

    def get_paginator(self, operation: str) -> Pages:
        return Pages([{"Items": self.channels if operation == "list_channels" else self.alerts}])

    def get_parameter(self, *, Name: str) -> dict[str, Any]:  # noqa: N803
        assert Name == "/fastm/ops/running-channels"
        return {"Parameter": {"Value": self.running}}

    def put_events(self, *, Entries: list[dict[str, str]]) -> dict[str, Any]:  # noqa: N803
        self.put.append(Entries)
        return {"FailedEntryCount": self.refused, "Entries": []}

    def start_query(self, **request: Any) -> dict[str, str]:
        self.queries.append(request)
        return {"queryId": "q-1"}

    def get_query_results(self, *, queryId: str) -> dict[str, Any]:  # noqa: N803
        return {"status": self.query_status.pop(0), "results": self.results}


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> FakeClients:
    fake = FakeClients()
    monkeypatch.setattr(aws, "client", lambda service: fake)
    monkeypatch.setattr(zero_ads_sessions.time, "sleep", lambda seconds: None)
    for name, value in {
        "FAST_PREFIX": "fastm",
        "EVENT_BUS": "fastm-fast-channels",
        "CENSUS_PARAMETER": "/fastm/ops/running-channels",
        "ADS_LOG_GROUP": "/aws/vendedlogs/mediatailor/fastm",
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    return fake


def alert(code: str, at: datetime, message: str = "") -> dict[str, Any]:
    return {
        "AlertCode": code,
        "AlertMessage": message or f"{code} on the channel",
        "Category": "SCHEDULING_ERROR" if code == "SCHEDULING_ERROR" else "PLAYBACK_WARNING",
        "LastModifiedTime": at,
        "ResourceArn": CHANNEL["Arn"],
    }


def test_alerts_changed_in_the_minute_before_the_poll_are_published_once(clients: FakeClients) -> None:
    clients.alerts = [
        alert("SCHEDULING_ERROR", SCHEDULED - timedelta(seconds=10)),
        alert("PROGRAM_ALERT", SCHEDULED - timedelta(seconds=30), "p1: MISSING_VOD_SOURCE"),
        alert("SCHEDULING_ERROR", SCHEDULED - timedelta(seconds=61)),  # the previous poll's
        alert("SCHEDULING_ERROR", SCHEDULED),  # the next poll's
        alert("SEGMENT_DURATION_MISMATCH", SCHEDULED - timedelta(seconds=5)),  # expected by design
    ]

    result = list_alerts.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context())

    assert result == {"published": 2}
    [entries] = clients.put
    assert [(e["Source"], e["DetailType"], e["EventBusName"]) for e in entries] == [
        ("fast-channels.fastm", "Channel Alert", "fastm-fast-channels")
    ] * 2
    details = [ChannelAlert.model_validate_json(e["Detail"]) for e in entries]
    assert [d.code for d in details] == ["SCHEDULING_ERROR", "PROGRAM_ALERT"]
    assert details[0].channel == "fastm-demo"
    assert details[0].last_modified == "2026-10-29T02:59:50.000Z"
    assert {d.correlation_id for d in details} == {"2026-10-29T03:00:00Z"}


def test_nothing_is_read_while_no_channel_runs(clients: FakeClients) -> None:
    clients.running = "0"
    clients.alerts = [alert("SCHEDULING_ERROR", SCHEDULED - timedelta(seconds=10))]

    assert list_alerts.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context()) == {"skipped": "no channel runs"}
    assert clients.put == []


def test_only_the_deployments_running_channels_are_polled(clients: FakeClients) -> None:
    clients.channels = [
        {**CHANNEL, "ChannelState": "STOPPED"},
        {**CHANNEL, "Tags": {"fast:app": "fast"}},
        {**CHANNEL, "Tags": {}},
    ]
    clients.alerts = [alert("SCHEDULING_ERROR", SCHEDULED - timedelta(seconds=10))]

    assert list_alerts.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context()) == {"published": 0}


def test_refused_alert_events_fail_the_poll_for_a_retry(clients: FakeClients) -> None:
    clients.alerts = [alert("SCHEDULING_ERROR", SCHEDULED - timedelta(seconds=10))]
    clients.refused = 1

    with pytest.raises(RuntimeError, match="refused 1 Channel Alert"):
        list_alerts.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context())


def test_zero_ad_sessions_of_the_hour_are_counted_and_logged(
    clients: FakeClients, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    clients.results = [
        [
            {"field": "sessionId", "value": "s-1"},
            {"field": "originId", "value": "fastm-demo"},
            {"field": "breaks", "value": "4"},
            {"field": "requests", "value": "0"},
        ]
    ]

    assert zero_ads_sessions.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context()) == {"sessions": 1}
    [query] = clients.queries
    assert query["logGroupNames"] == ["/aws/vendedlogs/mediatailor/fastm"]
    assert query["endTime"] - query["startTime"] == 3600
    assert query["queryString"] == zero_ads_sessions.QUERY
    [record] = [r for r in caplog.records if "requested no ads" in r.getMessage()]
    assert (record.session_id, record.configuration, record.breaks) == ("s-1", "fastm-demo", "4")  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert [json.loads(line)["ZeroAdsSessions"] for line in out.splitlines() if '"_aws"' in line] == [[1.0]]


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "Timeout"])
def test_a_failed_query_fails_the_run(clients: FakeClients, status: str) -> None:
    clients.query_status = [status]

    with pytest.raises(zero_ads_sessions.QueryError, match=status):
        zero_ads_sessions.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context())


def test_a_query_that_never_completes_fails_the_run(clients: FakeClients, monkeypatch: pytest.MonkeyPatch) -> None:
    clients.query_status = ["Running"] * 5
    monkeypatch.setattr(zero_ads_sessions, "DEADLINE_SECONDS", -1)

    with pytest.raises(zero_ads_sessions.QueryError, match="did not complete"):
        zero_ads_sessions.handler({"scheduled_time": "2026-10-29T03:00:00Z"}, Context())


def test_the_query_counts_distinct_markers_and_requests_per_session() -> None:
    query = zero_ads_sessions.QUERY

    assert 'filter eventType in ["AD_MARKER_FOUND", "MAKING_ADS_REQUEST"]' in query
    assert "count_distinct(marker) as breaks" in query
    assert "filter breaks >= 3 and requests = 0" in query
