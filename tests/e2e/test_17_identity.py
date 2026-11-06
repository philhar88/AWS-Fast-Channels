"""Viewer identity (``modules.identity``, ADR-0018): the viewer pool, ``POST /playback`` and ``POST /redeem`` on a
deployed stage.

Runs only when the stage has the Identity stack (``STAGE=fastm`` or ``fastci``). The test creates one viewer of its
own in the pool (``e2e-<run id>@personas.example.com``), signs it in with a password as the viewer app does, mints
tickets for the stage's first monetized channel, and redeems them as MediaTailor's PRE_SESSION Function would. It
runs the CDN's edge rebuild on crafted events (``TestFunction``) and asks the CDN for an explicit session init. It
deletes the viewer and its ticket rows at the end. It plays nothing: sessions on a running channel, with the
PRE_SESSION redeem and the Pod Decision Service's binding, are ``test_19_identity_session.py``'s.
"""

import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx
import pytest
import seed_personas
from _stack import MODULE_OUTPUT_KEYS, Module, Stage
from botocore.exceptions import ClientError
from harness_identity import SignedIn, claims, mint, parameter, sign_in, ticket_of
from harness_identity import viewer as signed_in_viewer

from fast_channels.core.identity.names import PERSONA_CLAIM, PLAYBACK_SCOPE, Parameter
from fast_channels.core.identity.ticket import TICKET
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.monetization.names import plan_key

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

pytestmark = pytest.mark.e2e

PERSONA: Final = "e2e-viewer"
SEEDED: Final = "e2e-seeded"
"""The persona the seeding test creates with ``scripts/seed_personas.py`` and deletes again."""
TIMEOUT: Final = httpx.Timeout(15.0)


@dataclass
class Identity:
    """The stage's Identity stack: its outputs and SSM parameters, and what the test created."""

    stage: Stage
    outputs: dict[str, str]
    minted: list[str] = field(default_factory=list)
    """Tickets the test minted (their rows are deleted at the end)."""

    def parameter(self, which: Parameter) -> str:
        return parameter(self.stage, which)

    @property
    def table(self) -> Table:
        return self.stage.session.resource("dynamodb", region_name=self.stage.region).Table(
            self.outputs["IdentityTable"]
        )

    def mint(self, token: str | None, body: Any) -> httpx.Response:
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
        response = httpx.post(f"{self.outputs['PlaybackApiUrl']}playback", headers=headers, json=body, timeout=TIMEOUT)
        if response.status_code == 200:
            self.minted.append(ticket_of(response.json()["url"]))
        return response

    def redeem(self, ticket: str, sid: str, pc: str) -> httpx.Response:
        return httpx.post(self.outputs["RedeemUrl"], json={"t": ticket, "sid": sid, "pc": pc}, timeout=TIMEOUT)

    def row(self, ticket: str) -> dict[str, Any]:
        item = self.table.get_item(Key={"pk": f"T#{ticket}"}, ConsistentRead=True).get("Item")
        assert item is not None, "no row for the ticket"
        return dict(item)


@pytest.fixture(scope="module")
def identity(stage: Stage) -> Iterator[Identity]:
    if stage.status(Module.IDENTITY) is None:
        pytest.skip(f"stage {stage.prefix} has no Identity stack (modules.identity is off)")
    found = Identity(stage, stage.outputs(Module.IDENTITY))
    assert not [key for key in MODULE_OUTPUT_KEYS[Module.IDENTITY] if not found.outputs.get(key)], found.outputs
    yield found
    for ticket in found.minted:
        found.table.delete_item(Key={"pk": f"T#{ticket}"})


@pytest.fixture(scope="module")
def viewer(identity: Identity, run_id: str) -> Iterator[SignedIn]:
    """A viewer of the pool, signed in with USER_PASSWORD_AUTH on the viewer app client."""
    assert identity.parameter(Parameter.CLIENT_ID) == identity.outputs["UserPoolClientId"]
    with signed_in_viewer(identity.stage, f"e2e-{run_id.lower()}@personas.example.com", persona=PERSONA) as found:
        yield found


@pytest.fixture(scope="module")
def plan(identity: Identity) -> dict[str, Any]:
    """The playback plan of the stage's first monetized channel."""
    ssm, s3 = identity.stage.session.client("ssm"), identity.stage.session.client("s3")
    bucket = ssm.get_parameter(Name=f"/{identity.stage.prefix}/release/media-bucket")["Parameter"]["Value"]
    key = plan_key(f"{identity.stage.prefix}-demo")  # the stage files monetize <prefix>-demo
    found: dict[str, Any] = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    return found


def test_the_access_token_carries_the_playback_scope_and_the_persona(viewer: SignedIn) -> None:
    access, id_token = claims(viewer.access_token), claims(viewer.id_token)
    assert access["token_use"] == "access" and PLAYBACK_SCOPE in access["scope"].split(), access.get("scope")
    assert access[PERSONA_CLAIM] == PERSONA
    assert id_token["token_use"] == "id" and "scope" not in id_token and id_token["custom:persona"] == PERSONA


def test_no_token_or_an_id_token_is_refused_with_cors(
    identity: Identity, viewer: SignedIn, plan: dict[str, Any]
) -> None:
    for token in (None, viewer.id_token, "not-a-jwt"):
        response = identity.mint(token, {"channel": plan["channel"]})
        assert response.status_code in {401, 403}, (token and token[:10], response.status_code, response.text)
        assert response.headers.get("access-control-allow-origin") == "*", "the browser sees the error"
    preflight = httpx.options(
        f"{identity.outputs['PlaybackApiUrl']}playback",
        headers={
            "Origin": "https://viewer.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
        timeout=TIMEOUT,
    )
    assert preflight.status_code in {200, 204} and preflight.headers["access-control-allow-origin"] == "*"


def test_a_signed_in_viewer_gets_a_single_use_ticket(
    identity: Identity, viewer: SignedIn, plan: dict[str, Any]
) -> None:
    started = time.time() * 1000
    response = identity.mint(viewer.access_token, {"channel": plan["channel"]})
    assert response.status_code == 200, response.text
    answer = response.json()
    url, ticket = answer["url"], ticket_of(answer["url"])
    assert url == f"{plan['playback']['hls']}?ads.t={ticket}" and TICKET.fullmatch(ticket)
    assert answer["expires_in"] == 120 and re.fullmatch(r"[0-9a-f-]{36}", answer["cid"])
    row = identity.row(ticket)
    kms = identity.stage.session.client("kms")
    vid = viewer_id(
        kms, key_id=identity.parameter(Parameter.VID_KEY), issuer=identity.parameter(Parameter.ISSUER), sub=viewer.sub
    )
    assert row["vid"] == vid, "the viewer's vid, computed from their sub with the stage's key"
    assert viewer.sub not in json.dumps(row, default=str), "the raw sub never reaches the row"
    assert (row["pc"], row["pm"], row["persona"], row["cid"]) == (
        plan["configuration"],
        "PSEUDONYMOUS",
        PERSONA,
        answer["cid"],
    )
    assert (
        int(row["redeem_by"]) - int(row["iat"]) == 120_000 and started - 5_000 <= int(row["iat"]) <= time.time() * 1000
    )
    assert "redeemedBy" not in row


def test_the_redeem_binds_a_ticket_to_one_session(identity: Identity, viewer: SignedIn, plan: dict[str, Any]) -> None:
    response = identity.mint(viewer.access_token, {"channel": plan["channel"]})
    ticket, pc = ticket_of(response.json()["url"]), plan["configuration"]
    first = identity.redeem(ticket, "e2e-session-1", pc)
    assert (first.status_code, first.json()) == (200, {"ok": True}), first.text
    row = identity.row(ticket)
    assert row["redeemedBy"] == "e2e-session-1" and row["bound_at"] == row["last_seen"]
    again = identity.redeem(ticket, "e2e-session-1", pc)
    assert again.status_code == 200, "a retried hook of the same session"
    assert identity.row(ticket)["bound_at"] == row["bound_at"], "the binding's start stays"
    replay = identity.redeem(ticket, "e2e-session-2", pc)
    assert (replay.status_code, replay.json()) == (409, {"ok": False}), "another session never binds a used ticket"
    assert identity.row(ticket)["redeemedBy"] == "e2e-session-1"
    other = identity.redeem(ticket, "e2e-session-1", f"{pc}x")
    assert other.status_code == 409, "a ticket binds only on the configuration it was minted for"
    for body in (
        {"t": ticket[:21], "sid": "s", "pc": pc},
        {"t": ticket, "sid": "s"},
        {"t": ticket, "sid": "s'", "pc": pc},
    ):
        refused = httpx.post(identity.outputs["RedeemUrl"], json=body, timeout=TIMEOUT)
        assert refused.status_code == 400, (body, refused.status_code)


def test_an_expired_or_unknown_ticket_is_not_bound(identity: Identity, viewer: SignedIn, plan: dict[str, Any]) -> None:
    response = identity.mint(viewer.access_token, {"channel": plan["channel"]})
    ticket, pc = ticket_of(response.json()["url"]), plan["configuration"]
    identity.table.update_item(
        Key={"pk": f"T#{ticket}"},
        UpdateExpression="SET redeem_by = :past",
        ExpressionAttributeValues={":past": int(time.time() * 1000) - 1_000},
    )
    assert identity.redeem(ticket, "e2e-session-3", pc).status_code == 409, "past its redeem window"
    assert "redeemedBy" not in identity.row(ticket)
    assert identity.redeem("u" * 22, "e2e-session-3", pc).status_code == 409, "no row"


def test_an_unknown_channel_or_a_bad_body_is_a_400(identity: Identity, viewer: SignedIn) -> None:
    for body, error in (
        ({"channel": f"{identity.stage.prefix}-nope"}, "unknown_channel"),
        ({"chan": 1}, "invalid_request"),
    ):
        response = identity.mint(viewer.access_token, body)
        assert (response.status_code, response.json()["error"]) == (400, error), response.text


def test_the_access_log_holds_no_address_or_ticket(identity: Identity, viewer: SignedIn, plan: dict[str, Any]) -> None:
    """Only with the account's API Gateway CloudWatch role (docs/identity.md#logs): without it nothing is logged."""
    session = identity.stage.session
    role = session.client("apigateway").get_account().get("cloudwatchRoleArn")
    try:
        assert role and session.client("iam").get_role(RoleName=role.rsplit("/", 1)[-1])
    except AssertionError, ClientError:
        pytest.skip("the account has no valid API Gateway CloudWatch role, so the API writes no access log")
    api = urlsplit(identity.outputs["PlaybackApiUrl"]).netloc.split(".")[0]
    destination = session.client("apigateway").get_stage(restApiId=api, stageName="v1")["accessLogSettings"]
    group = destination["destinationArn"].split(":log-group:")[-1].removesuffix(":*")
    started = int(time.time() * 1000)
    ticket = ticket_of(identity.mint(viewer.access_token, {"channel": plan["channel"]}).json()["url"])
    messages: list[str] = []
    for _ in range(24):  # access logs arrive within about a minute
        page = session.client("logs").filter_log_events(logGroupName=group, startTime=started)
        messages = [event["message"] for event in page["events"]]
        if any('"/playback"' in message for message in messages):
            break
        time.sleep(5)
    assert messages, f"no access log in {group} after 2 minutes"
    assert not [m for m in messages if ticket in m or viewer.sub in m or re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", m)]


def test_a_seeded_persona_signs_in_and_mints_through_the_harness(identity: Identity, plan: dict[str, Any]) -> None:
    """``scripts/seed_personas.py`` and ``harness_identity``, as the other e2e tests use them."""
    entry = {"id": SEEDED, "email": f"{SEEDED}@personas.example.com"}
    try:
        [seeded] = seed_personas.seed(identity.stage, [entry])
        token = sign_in(identity.stage, SEEDED)
        assert claims(token)[PERSONA_CLAIM] == SEEDED
        answer = mint(identity.stage, token, plan["channel"])
        identity.minted.append(ticket := ticket_of(answer["url"]))
        assert identity.row(ticket)["vid"] == seeded["vid"], "the script prints the vid the mint writes"
    finally:
        assert seed_personas.delete(identity.stage, [entry]) == [SEEDED]


TICKET_1, TICKET_2 = "A" * 11 + "b" * 11, "C" * 11 + "d" * 11
EDGE_CASES: Final[list[tuple[str, str, dict[str, str | list[str]], dict[str, str] | int]]] = [
    # (label, path, query, expected): P0-05's crafted events; expected is the query left, or the refusal's status
    ("a ticket and junk", "init", {"ads.t": TICKET_1, "ads.x": "junk", "aws.logMode": "DEBUG"}, {"ads.t": TICKET_1}),
    ("two tickets", "init", {"ads.t": [TICKET_1, TICKET_2]}, {}),
    ("21 characters", "init", {"ads.t": TICKET_1[:21]}, {}),
    ("overrides only", "init", {"aws.availSuppressionMode": "OFF"}, {}),
    ("an extra segment", "extra", {"ads.t": TICKET_1}, 400),
    ("explicit session init", "session", {"ads.t": TICKET_1}, 403),
    ("a child playlist", "child", {"aws.sessionId": "s"}, {"aws.sessionId": "s"}),
]


def _edge_paths(plan: dict[str, Any]) -> dict[str, str]:
    path = urlsplit(plan["playback"]["hls"]).path  # /v1/master/<hash>/<configuration>/<name>.m3u8
    _, _, _, account, configuration, name = path.split("/")
    return {
        "init": path,
        "extra": f"/v1/master/{account}/{configuration}/extra/{name}",
        "session": f"/v1/session/{account}/{configuration}/{name}",
        "child": f"/v1/manifest/{account}/{configuration}/00000000-0000-0000-0000-000000000000/0.m3u8",
    }


def _event_query(query: dict[str, str | list[str]]) -> dict[str, Any]:
    built: dict[str, Any] = {}
    for name, value in query.items():
        values = value if isinstance(value, list) else [value]
        multi = {"multiValue": [{"value": v} for v in values]} if len(values) > 1 else {}
        built[name] = {"value": values[0], **multi}
    return built


def test_the_deployed_edge_rebuild_answers_the_crafted_events(identity: Identity, plan: dict[str, Any]) -> None:
    """CloudFront's ``TestFunction`` on the published function (its source's unit tests are
    ``infra/tests/test_edge_rebuild.py``)."""
    cloudfront = identity.stage.session.client("cloudfront")
    name = f"{identity.stage.prefix}-identity-edge"
    etag = cloudfront.describe_function(Name=name, Stage="LIVE")["ETag"]
    paths = _edge_paths(plan)
    for label, path, query, expected in EDGE_CASES:
        event = {
            "version": "1.0",
            "context": {"eventType": "viewer-request"},
            "viewer": {"ip": "198.51.100.10"},
            "request": {"method": "GET", "uri": paths[path], "querystring": _event_query(query), "headers": {}},
        }
        result = cloudfront.test_function(
            Name=name, IfMatch=etag, Stage="LIVE", EventObject=json.dumps(event).encode()
        )["TestResult"]
        assert not result.get("FunctionErrorMessage"), (label, result)
        output = json.loads(result["FunctionOutput"])
        if isinstance(expected, int):
            assert output["response"]["statusCode"] == expected, (label, output)
        else:
            left = {k: v["value"] for k, v in output["request"]["querystring"].items()}
            assert left == expected, (label, output)


def test_the_cdn_refuses_explicit_session_initialization(plan: dict[str, Any]) -> None:
    """``POST /v1/session`` is refused by the behaviour's methods, ``GET`` by the function, both 403; a non-canonical
    init path is a 400. None of them reaches MediaTailor."""
    parts = urlsplit(plan["playback"]["hls"])
    origin, paths = f"{parts.scheme}://{parts.netloc}", _edge_paths(plan)
    with httpx.Client(timeout=TIMEOUT) as http:
        assert http.post(f"{origin}{paths['session']}", json={}).status_code == 403
        assert http.get(f"{origin}{paths['session']}").status_code == 403
        assert http.get(f"{origin}{paths['extra']}?ads.t={TICKET_1}").status_code == 400
