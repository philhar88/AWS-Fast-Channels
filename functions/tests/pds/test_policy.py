"""``POST /policy`` behind the internal load balancer: ALB events in, the patch and one decision record out."""

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from fast_channels.core.pds.snapshot import EMPTY, Snapshot, SnapshotBase, build, demo_demand
from fast_channels.functions.pds import policy
from fast_channels.functions.settings import settings

SID = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d"
VIEWER = "81.2.69.160"
XFF = f"{VIEWER}, 130.176.12.7, 3.81.20.30, 130.176.40.9"
TV = "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 (KHTML, like Gecko) TV Safari/537.36"
SNAPSHOT = build(
    SnapshotBase(demand=demo_demand("fastm")),
    origin_facing=("130.176.0.0/18",),
    ip_ranges="1",
    now=datetime(2026, 10, 29, tzinfo=UTC),
)


@dataclass
class Context:
    function_name: str = "fastm-pds-policy"
    function_version: str = "7"
    memory_limit_in_mb: int = 1024
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:fastm-pds-policy:live"
    aws_request_id: str = "request-1"
    log_group_name: str = "/aws/lambda/fastm-pds-policy"
    log_stream_name: str = "stream"

    def get_remaining_time_in_millis(self) -> int:
        return 3000


class _Store:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot

    def get(self) -> Snapshot:
        return self.snapshot


@pytest.fixture
def snapshot(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Store]:
    store = _Store(SNAPSHOT)
    monkeypatch.setattr(policy, "store", lambda: store)
    yield store


def question(**fields: Any) -> dict[str, Any]:
    body = {
        "v": 1,
        "t": "",
        "sid": SID,
        "pc": "fastm-demo-p-a",
        "fv": "r0001",
        "evid": "820461826",
        "upid": "tears-of-steel",
        "dur_ms": 60060,
        "ua": TV,
        "xff": XFF,
        "cfva": "3.81.20.30:46532",
        "ctx": "",
        "probe": False,
    }
    return body | fields


def alb(body: str | None, *, path: str = "/policy", method: str = "POST", encode: bool = False) -> dict[str, Any]:
    return {
        "requestContext": {"elb": {"targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:1:targetgroup/pds/1"}},
        "httpMethod": method,
        "path": path,
        "queryStringParameters": {},
        "headers": {"content-type": "application/json", "host": "pds.fastm.fast.internal"},
        "body": base64.b64encode(body.encode()).decode() if encode and body else body,
        "isBase64Encoded": encode,
    }


def records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """The decision records logged (Powertools passes ``extra`` keys as log record attributes)."""
    return [vars(r) for r in caplog.records if getattr(r, "record", None) == "pds.policy"]


@pytest.mark.usefixtures("snapshot")
def test_a_contextual_session_gets_the_demo_bidder_and_its_device(caplog: pytest.LogCaptureFixture) -> None:
    response = policy.handler(alb(json.dumps(question())), Context())

    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    answer = json.loads(response["body"])
    assert answer == {
        "v": 1,
        "decision_id": f"{SID}.820461826",
        "mode": "CONTEXTUAL",
        "reason": "missing",
        "bidders": {"amt": {"placementId": "fastm-demo"}},
        "regs": {"coppa": 0, "gpp": None, "gpp_sid": None},
        "device": {"ip": VIEWER, "ipv6": None, "devicetype": 3},
        "app_content": None,
        "user": None,
        "fpd": {"bidders": ["ownbook"]},
        "deals": [],
    }
    [record] = records(caplog)
    assert (record["decision_id"], record["mode"], record["ip_rule"], record["snapshot"]) == (
        f"{SID}.820461826",
        "CONTEXTUAL",
        "ok",
        SNAPSHOT.version,
    )
    assert policy.logger.get_correlation_id() == f"{SID}.820461826"
    assert VIEWER not in json.dumps(record, default=str)


@pytest.mark.usefixtures("snapshot")
@pytest.mark.parametrize(
    ("fields", "mode", "reason", "bidders", "coppa"),
    [
        ({"pc": "fastm-demo-p-z", "probe": True}, "SAFE", "probe", {}, 0),
        ({"pc": "fastm-demo-p-k"}, "KIDS", "kids", {}, 1),
        ({"t": "AbCdEfGhIjKlMnOpQrStUv"}, "CONTEXTUAL", "unknown", {"amt": {"placementId": "fastm-demo"}}, 0),
        ({"t": "short"}, "CONTEXTUAL", "invalid", {"amt": {"placementId": "fastm-demo"}}, 0),
    ],
)
def test_modes(fields: dict[str, Any], mode: str, reason: str, bidders: dict[str, Any], coppa: int) -> None:
    answer = json.loads(policy.handler(alb(json.dumps(question(**fields)), encode=True), Context())["body"])

    assert (answer["mode"], answer["reason"], answer["bidders"], answer["regs"]["coppa"]) == (
        mode,
        reason,
        bidders,
        coppa,
    )


def test_without_a_snapshot_no_third_party_and_no_address(snapshot: _Store) -> None:
    snapshot.snapshot = EMPTY

    answer = json.loads(policy.handler(alb(json.dumps(question())), Context())["body"])

    assert (answer["mode"], answer["bidders"], answer["device"]["ip"]) == ("CONTEXTUAL", {}, None)


@pytest.mark.usefixtures("snapshot")
@pytest.mark.parametrize("body", ["", "not json", json.dumps(question(v=2)), json.dumps(question(evid=""))])
def test_a_request_outside_the_contract_is_refused(body: str, capfd: pytest.CaptureFixture[str]) -> None:
    response = policy.handler(alb(body), Context())

    assert (response["statusCode"], json.loads(response["body"])) == (400, {"error": "invalid request"})
    assert "AbCd" not in capfd.readouterr().out


@pytest.mark.usefixtures("snapshot")
def test_the_ticket_is_never_logged(capfd: pytest.CaptureFixture[str]) -> None:
    ticket = "AbCdEfGhIjKlMnOpQrStUv"
    policy.handler(alb(json.dumps(question(t=ticket))), Context())
    policy.handler(alb(json.dumps(question(t=ticket, extra="x"))), Context())

    assert ticket not in capfd.readouterr().out


@pytest.mark.usefixtures("snapshot")
def test_other_paths_and_methods_are_not_found() -> None:
    assert policy.handler(alb("{}", path="/select"), Context())["statusCode"] == 404
    assert policy.handler(alb(None, method="GET"), Context())["statusCode"] == 404


def test_a_failure_is_a_500(snapshot: _Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(policy, "decide", broken)

    response = policy.handler(alb(json.dumps(question())), Context())

    assert (response["statusCode"], json.loads(response["body"])) == (500, {"error": "internal"})


def test_the_store_reads_the_configured_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDS_CONFIG_BUCKET", "fastm-pds-config")
    settings.cache_clear()
    policy.store.cache_clear()
    try:
        assert policy.store()._bucket == "fastm-pds-config"
    finally:
        policy.store.cache_clear()
        settings.cache_clear()
