"""The ad panel API: a viewer's own session's decisions only, from one Logs Insights query."""

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from fast_channels.functions.viewer import ad_panel

SID = "0f8e2b5c-3d0a-4a5e-9c1b-2f6d7e8a9b0c"
VID = "6b1f0c2d9e8a7b6c5d4e3f2a1b0c9d8e"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"


class _Context:
    function_name = "fastm-viewer-ad-panel"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fastm-viewer-ad-panel"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self) -> int:
        return 10_000


class FakeLogs:
    """Logs Insights: one query that runs once, then completes with ``rows``."""

    def __init__(self, rows: list[list[dict[str, str]]], statuses: tuple[str, ...] = ("Running", "Complete")) -> None:
        self.rows, self.statuses = rows, list(statuses)
        self.started: list[dict[str, Any]] = []
        self.stopped: list[str] = []

    def start_query(self, **kwargs: Any) -> dict[str, str]:
        self.started.append(kwargs)
        return {"queryId": "q-1"}

    def get_query_results(self, *, queryId: str) -> dict[str, Any]:  # noqa: N803 - the API's name
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "results": self.rows, "statistics": {"bytesScanned": 1024.0}}

    def stop_query(self, *, queryId: str) -> None:  # noqa: N803
        self.stopped.append(queryId)


def row(record: dict[str, Any], at: str = "2026-11-05 20:04:00.120") -> list[dict[str, str]]:
    return [{"field": "@timestamp", "value": at}, {"field": "@message", "value": json.dumps(record)}]


POLICY = {
    "record": "pds.policy",
    "decision_id": f"{SID}.101",
    "sid": SID,
    "evid": "101",
    "probe": False,
    "mode": "FULL",
    "reason": "bound",
    "dur_ms": 60060,
    "vid": VID,
}
SELECT = {
    "record": "pds.select",
    "decision_id": f"{SID}.101",
    "sid": SID,
    "evid": "101",
    "probe": False,
    "mode": "FULL",
    "dur_ms": 60060,
    "reason": "partial",
    "pod": ["b"],
    "filled_ms": 30030,
    "bids": [
        {
            "ref": "b",
            "bidder": "amt",
            "price": 35,
            "house": False,
            "dealid": None,
            "vast_dur_ms": 30030,
            "outcome": "won",
        }
    ],
    "below_floor": 0,
    "tier": "light",
    "ad_load": {"policy": "light", "reason": "tier", "max_ads": 1, "bar_cpm": 20, "slots": 2},
}


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DECISION_LOG_GROUP", "/aws/ecs/fastm-pds")
    monkeypatch.setenv("VID_KEY_ID", "arn:aws:kms:us-east-1:123456789012:key/0000")
    monkeypatch.setattr(
        ad_panel, "caller_vid", lambda issuer, sub: VID if (issuer, sub) == (ISSUER, "sub-dana") else "other"
    )
    monkeypatch.setattr(ad_panel.time, "sleep", lambda _seconds: None)
    ad_panel._cached.clear()


def logs(monkeypatch: pytest.MonkeyPatch, fake: FakeLogs) -> FakeLogs:
    monkeypatch.setattr(ad_panel, "client", lambda service: fake)
    return fake


def call(sid: str = SID, sub: str | None = "sub-dana") -> dict[str, Any]:
    claims = {"iss": ISSUER, "sub": sub, "scope": "fast/playback"} if sub else {}
    event = {
        "version": "2.0",
        "routeKey": "GET /sessions/{sid}/breaks",
        "rawPath": f"/sessions/{sid}/breaks",
        "rawQueryString": "",
        "headers": {"authorization": "Bearer t"},
        "pathParameters": {"sid": sid},
        "requestContext": {
            "http": {"method": "GET", "path": f"/sessions/{sid}/breaks", "protocol": "HTTP/1.1", "sourceIp": "3.3.3.3"},
            "requestId": "r",
            "routeKey": "GET /sessions/{sid}/breaks",
            "stage": "$default",
            "authorizer": {"jwt": {"claims": claims, "scopes": ["fast/playback"]}},
        },
        "isBase64Encoded": False,
    }
    return ad_panel.handler(event, _Context())  # type: ignore[arg-type]


def body(answer: dict[str, Any]) -> Any:
    return json.loads(answer["body"])


def test_the_viewer_gets_their_own_sessions_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = logs(monkeypatch, FakeLogs([row(POLICY), row(SELECT, "2026-11-05 20:04:00.250")]))

    answer = call()

    assert answer["statusCode"] == 200
    assert answer["headers"]["Cache-Control"] == "private, no-store"
    [decision] = body(answer)["breaks"]
    assert (decision["tier"], decision["ad_load"]["max_ads"], len(decision["ads"])) == ("light", 1, 1)
    [query] = fake.started
    assert query["logGroupName"] == "/aws/ecs/fastm-pds"
    assert f'sid = "{SID}"' in query["queryString"]


def test_another_viewers_session_and_an_anonymous_one_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    logs(monkeypatch, FakeLogs([row(POLICY), row(SELECT)]))
    assert call(sub="sub-marcus")["statusCode"] == 403

    ad_panel._cached.clear()
    logs(monkeypatch, FakeLogs([row({**POLICY, "mode": "CONTEXTUAL", "reason": "missing", "vid": None})]))
    assert call()["statusCode"] == 403


def test_nothing_decided_yet_is_an_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    logs(monkeypatch, FakeLogs([row(SELECT)]))
    assert body(call()) == {"sid": SID, "breaks": []}


def test_a_bad_session_id_or_no_viewer_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = logs(monkeypatch, FakeLogs([]))
    assert call(sid="not-a-session")["statusCode"] == 400
    assert call(sub=None)["statusCode"] == 401
    assert fake.started == []


def test_a_slow_or_failed_query_is_a_503(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = logs(monkeypatch, FakeLogs([], statuses=("Running",)))
    ticks = iter(range(0, 1000, 5))
    monkeypatch.setattr(ad_panel.time, "monotonic", lambda: float(next(ticks)))
    assert call()["statusCode"] == 503
    assert fake.stopped == ["q-1"]

    ad_panel._cached.clear()
    logs(monkeypatch, FakeLogs([], statuses=("Failed",)))
    assert call()["statusCode"] == 503


def test_no_sidecar_log_group_yet_is_an_empty_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLogs([])

    def missing(**_kwargs: Any) -> dict[str, str]:
        raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no group"}}, "StartQuery")

    fake.start_query = missing  # type: ignore[method-assign]
    logs(monkeypatch, fake)
    assert body(call()) == {"sid": SID, "breaks": []}


def test_a_session_is_queried_once_per_cache_period(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = logs(monkeypatch, FakeLogs([row(POLICY)]))
    ad_panel.session_records(SID, now=1000.0)
    ad_panel.session_records(SID, now=1010.0)
    ad_panel.session_records(SID, now=1016.0)
    assert len(fake.started) == 2
