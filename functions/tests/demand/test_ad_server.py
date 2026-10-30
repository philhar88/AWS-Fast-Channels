"""The mock ad server behind its HTTP API: only requests with an origin secret are answered, with the Prebid line's
Wrapper to the cached pod or the house line's."""

import json
from typing import Any

import pytest

from fast_channels.functions.demand import ad_server

CACHE = "https://d222222abcdef8.cloudfront.net/cache"
HOUSE = "https://d111111abcdef8.cloudfront.net/ads/standard_843ed1d05a/vast.xml"
IMPRESSION = "https://d111111abcdef8.cloudfront.net/ads/b.gif?evt=adserver"
UUID = "0b9a7c3e-1111-4222-8333-444455556666"


class _Context:
    function_name = "fastm-demand-ad-server"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:fastm-demand-ad-server"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self) -> int:
        return 3000


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_URL", CACHE)
    monkeypatch.setenv("HOUSE_URL", HOUSE)
    monkeypatch.setenv("IMPRESSION_URL", IMPRESSION)
    monkeypatch.setattr(ad_server, "origin_secrets", lambda: ["slot-a-value", "slot-b-value"])


def _event(query: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "version": "2.0",
        "routeKey": "GET /ads",
        "rawPath": "/ads",
        "rawQueryString": "&".join(f"{k}={v}" for k, v in query.items()),
        "headers": headers or {},
        "queryStringParameters": query,
        "requestContext": {
            "http": {"method": "GET", "path": "/ads", "protocol": "HTTP/1.1", "sourceIp": "3.3.3.3"},
            "requestId": "r",
            "routeKey": "GET /ads",
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }


def _call(query: dict[str, str], secret: str | None = "slot-b-value") -> dict[str, Any]:
    headers = {"x-fast-origin": secret} if secret is not None else {}
    return ad_server.handler(_event(query, headers), _Context())  # type: ignore[arg-type]


def test_a_prebid_line_answers_the_wrapper_to_the_cached_pod(caplog: pytest.LogCaptureFixture) -> None:
    answer = _call(
        {"pc": "fastm-demo-p-a", "sid": "s", "evid": "7", "hb_pb": "35.00", "hb_uuid": UUID, "hb_bidder": "fast-ctv"}
    )

    assert answer["statusCode"] == 200
    assert answer["headers"]["Content-Type"] == "application/xml"
    assert f"<VASTAdTagURI><![CDATA[{CACHE}?uuid={UUID}]]></VASTAdTagURI>" in answer["body"]
    [decision] = [vars(r) for r in caplog.records if r.getMessage() == "ad decision"]
    assert (decision["line"], decision["uuid8"], decision["pc"]) == ("prebid-35.00", "0b9a7c3e", "fastm-demo-p-a")
    assert ad_server.logger.get_correlation_id() == "s.7"
    logged = json.dumps(decision, default=str)
    assert "slot-b-value" not in logged and UUID not in logged


def test_no_targeting_is_the_house_line() -> None:
    answer = _call({"pc": "fastm-demo-p-a", "hb_pb": "", "hb_uuid": "", "hb_bidder": ""})

    assert answer["statusCode"] == 200
    assert f"<VASTAdTagURI><![CDATA[{HOUSE}]]></VASTAdTagURI>" in answer["body"]


@pytest.mark.parametrize("secret", [None, "", "wrong", "slot-b-valu", "slot-b-value-and-more"])
def test_without_the_origin_secret_nothing_is_answered(secret: str | None) -> None:
    answer = _call({"hb_pb": "35.00", "hb_uuid": UUID, "hb_bidder": "fast-ctv"}, secret=secret)

    assert answer["statusCode"] == 403
    assert answer["body"] == ""


def test_both_slots_are_valid() -> None:
    assert ad_server.authorized("slot-a-value") and ad_server.authorized("slot-b-value")
    assert not ad_server.authorized(None)
