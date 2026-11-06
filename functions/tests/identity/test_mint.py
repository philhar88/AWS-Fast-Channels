"""``POST /playback``: the mint answers a single-use ticket URL for a signed-in viewer's access token, and nothing for
anything else; the pre-token-generation trigger puts the scope and the persona in the access token."""

import hashlib
import hmac
import io
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from fast_channels.core.identity.vid import message, vid_from_mac
from fast_channels.functions import aws
from fast_channels.functions.identity import mint, pre_token
from fast_channels.functions.settings import settings

type Stub = Callable[[str], Stubber]
TABLE = "fastm-identity"
BUCKET = "fastm-media"
KEY = "arn:aws:kms:us-east-1:123456789012:key/vid"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
SUB = "0f6b4c7e-1a2b-4c3d-9e8f-001122334455"
TICKET = "Tk" * 11
CID = "01938f6e-0000-7000-8000-000000000001"
NOW_NS = 1_793_000_000_000_000_000
SECRET = b"k" * 32
URL = "https://d111111abcdef8.cloudfront.net/v1/master/0123abcd/fastm-demo-p-b/hls.m3u8"
PLAN = {
    "v": 1,
    "channel": "fastm-demo",
    "configuration": "fastm-demo-p-b",
    "release": "r0014",
    "playback": {"hls": URL},
    "updated": "2026-11-05T10:00:00Z",
}


@dataclass
class Context:
    function_name: str = "mint"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:mint"
    aws_request_id: str = "request-1"


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in {
        "FAST_PREFIX": "fastm",
        "MEDIA_BUCKET": BUCKET,
        "IDENTITY_TABLE": TABLE,
        "VID_KEY": KEY,
        "RELEASE_CHANNELS": "fastm-demo",
        "REDEEM_WINDOW_MS": "120000",
    }.items():
        monkeypatch.setenv(name, value)
    settings.cache_clear()
    mint._channels.cache_clear()
    monkeypatch.setattr(mint, "mint", lambda: TICKET)
    monkeypatch.setattr(mint.uuid, "uuid7", lambda: CID)
    monkeypatch.setattr(mint.time, "time_ns", lambda: NOW_NS)
    yield
    settings.cache_clear()
    mint._channels.cache_clear()


@pytest.fixture
def stub() -> Iterator[Stub]:
    clients = {"s3": aws.client("s3"), "kms": aws.client("kms"), "dynamodb": aws.table(TABLE).meta.client}
    stubbers = {name: Stubber(client) for name, client in clients.items()}
    for stubber in stubbers.values():
        stubber.activate()
    try:
        yield stubbers.__getitem__
    finally:
        for stubber in stubbers.values():
            stubber.deactivate()
    for stubber in stubbers.values():
        stubber.assert_no_pending_responses()


def event(body: Any, claims: dict[str, str] | None = None, method: str = "POST") -> dict[str, Any]:
    access = {
        "sub": SUB,
        "iss": ISSUER,
        "token_use": "access",
        "scope": "fast/playback",
        "client_id": "client",
        "fast:persona": "dana",
    }
    return {
        "resource": "/playback",
        "path": "/playback",
        "httpMethod": method,
        "headers": {"Content-Type": "application/json", "Origin": "https://viewer.example"},
        "multiValueHeaders": {},
        "queryStringParameters": None,
        "pathParameters": None,
        "requestContext": {
            "resourcePath": "/playback",
            "httpMethod": method,
            "stage": "v1",
            "requestId": "r-1",
            "authorizer": {"claims": access if claims is None else claims},
        },
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


def plan_response(stub: Stub, plan: dict[str, Any] | None = PLAN) -> None:
    where = {"Bucket": BUCKET, "Key": "plan/playback/fastm-demo.json"}
    if plan is None:
        stub("s3").add_client_error("get_object", "NoSuchKey", "no plan", expected_params=where)
        return
    data = json.dumps(plan).encode()
    stub("s3").add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data))}, where)


def answer(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return response["statusCode"], json.loads(response["body"])


def test_a_signed_in_viewer_gets_a_single_use_ticket_url(stub: Stub) -> None:
    plan_response(stub)
    mac = hmac.new(SECRET, message(ISSUER, SUB), hashlib.sha256).digest()
    stub("kms").add_response(
        "generate_mac",
        {"Mac": mac, "KeyId": KEY, "MacAlgorithm": "HMAC_SHA_256"},
        {"KeyId": KEY, "MacAlgorithm": "HMAC_SHA_256", "Message": message(ISSUER, SUB)},
    )
    now_ms = NOW_NS // 1_000_000
    stub("dynamodb").add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": {  # the resource API's values: the stub sees them before serialisation
                "pk": f"T#{TICKET}",
                "pc": "fastm-demo-p-b",
                "pm": "PSEUDONYMOUS",
                "cid": CID,
                "iat": now_ms,
                "redeem_by": now_ms + 120_000,
                "ttl": now_ms // 1000 + 25 * 3600,
                "vid": vid_from_mac(mac),
                "persona": "dana",
            },
            "ConditionExpression": "attribute_not_exists(pk)",
        },
    )
    response = mint.handler(event({"channel": "fastm-demo"}), Context())
    status, body = answer(response)
    assert status == 200, body
    assert body == {"url": f"{URL}?ads.t={TICKET}", "cid": CID, "expires_in": 120}
    assert response["multiValueHeaders"]["Access-Control-Allow-Origin"] == ["*"]


def test_an_id_token_or_a_token_without_a_subject_is_refused(stub: Stub) -> None:
    for claims in ({"sub": SUB, "iss": ISSUER, "token_use": "id"}, {"token_use": "access", "iss": ISSUER}, {}):
        status, body = answer(mint.handler(event({"channel": "fastm-demo"}, claims), Context()))
        assert (status, body["error"]) == (403, "forbidden"), claims


def test_a_bad_body_or_an_unknown_channel_is_a_400(stub: Stub) -> None:
    for body in ("", "not json", {"channel": "fastm-demo", "profile": "kids"}, {"channel": "../x"}, {}):
        status, answered = answer(mint.handler(event(body), Context()))
        assert (status, answered["error"]) == (400, "invalid_request"), body
    status, answered = answer(mint.handler(event({"channel": "fastm-other"}), Context()))
    assert (status, answered["error"]) == (400, "unknown_channel")


def test_a_channel_never_released_is_a_503_and_mints_nothing(stub: Stub) -> None:
    plan_response(stub, None)
    status, body = answer(mint.handler(event({"channel": "fastm-demo"}), Context()))
    assert (status, body["error"]) == (503, "not_released")


def test_a_malformed_persona_claim_is_left_out() -> None:
    assert mint._persona({"fast:persona": "Dana; drop"}) is None
    assert mint._persona({}) is None
    assert mint._persona({"fast:persona": "marcus"}) == "marcus"


def test_the_trigger_adds_the_scope_and_a_well_formed_persona() -> None:
    assert pre_token.overrides({"custom:persona": "ben", "email": "ben@personas.example.com"}) == {
        "accessTokenGeneration": {"scopesToAdd": ["fast/playback"], "claimsToAddOrOverride": {"fast:persona": "ben"}}
    }
    assert pre_token.overrides({"custom:persona": "BEN!"}) == {
        "accessTokenGeneration": {"scopesToAdd": ["fast/playback"]}
    }
    event = {
        "version": "2",
        "triggerSource": "TokenGeneration_Authentication",
        "request": {"userAttributes": {"custom:persona": "sam"}},
        "response": {},
    }
    result = pre_token.handler(event, Context())
    assert result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"] == {
        "fast:persona": "sam"
    }
    assert "idTokenGeneration" not in result["response"]["claimsAndScopeOverrideDetails"], ANY
