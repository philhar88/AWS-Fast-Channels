"""The identity API's routes (DESIGN-v2.2 §5.1; ADR-0018): playback tickets for signed-in viewers and guests, and the
signed-in viewer's consent.

- ``POST /playback``: a signed-in viewer's single-use playback ticket. API Gateway REST runs the viewer pool's Cognito
  authorizer first: only an access token carrying the scope ``fast/playback`` gets here (an ID token never carries
  it). The ticket's row (``core.identity.ticket``) holds the ``vid``, a KMS HMAC of the token's issuer and subject
  (``core.identity.vid``), and the demo persona from the token's ``fast:persona`` claim (the pre-token-generation
  trigger copies ``custom:persona``). Not the privacy mode: the Pod Decision Service reads the viewer's consent on
  every break.
- ``POST /playback/guest``: a guest's ticket, without an authorizer (a WAF rate rule per address guards it). The body
  may carry the consent platform's GPP string and sections and ``gpc``; ``Sec-GPC: 1`` counts as ``gpc``. The row has
  no ``vid``: its mode is CONTEXTUAL, or OPTED_OUT with Global Privacy Control (``core.identity.consent.guest_mode``),
  and the GPP string travels to Prebid Server.
- ``GET /consent`` and ``PUT /consent``: the signed-in viewer's consent row ``C#<vid>`` (``core.identity.consent``),
  the store the Pod Decision Service follows. Without a row a signed-in viewer's breaks are SAFE.

A ticket names the channel's stable sibling, from its playback plan (``plan/playback/<channel>.json``, which the
Release Controller rewrites at every promotion and rollback), and the answer is the plan's CDN playback URL with
``?ads.t=<t>``. The ticket is the only viewer value in the URL. A player opens it within the redeem window, in one
session; every new play asks for a new ticket. Anything else (an unknown channel, a channel never released) is an
error the player answers by playing the plan's anonymous URL.

Nothing about the viewer is logged but the correlation id ``cid``, the channel and the configuration: never the
ticket, the ``vid``, a GPP string or the token's claims.
"""

import json
import time
import uuid
from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig, Response
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fast_channels.core.identity import consent as consents
from fast_channels.core.identity.consent import ConsentChoice, ConsentRecord, guest_mode
from fast_channels.core.identity.names import PERSONA_CLAIM
from fast_channels.core.identity.ticket import (
    GPP_PATTERN,
    GPP_SECTIONS_MAX,
    REDEEM_WINDOW_MS,
    PrivacyMode,
    mint,
    ticket_row,
)
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.monetization.names import plan_key
from fast_channels.core.monetization.release import PlaybackPlan
from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
app = APIGatewayRestResolver(
    cors=CORSConfig(allow_origin="*", allow_headers=["Authorization", "Content-Type"], max_age=600)
)
GPC_HEADER: Final = "sec-gpc"
JSON: Final = "application/json"
PREFERRED_OUTPUT: Final = "hls"
PERSONA: Final = r"^[a-z][a-z0-9-]{0,31}$"


class PlaybackRequest(BaseModel):
    """The body of ``POST /playback``."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class GuestRequest(BaseModel):
    """The body of ``POST /playback/guest``."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    gpp: str | None = Field(default=None, pattern=GPP_PATTERN)
    gpp_sid: tuple[int, ...] = Field(default=(), max_length=GPP_SECTIONS_MAX)
    gpc: bool = False


class _Persona(BaseModel):
    persona: str = Field(pattern=PERSONA)


def _error(status: int, error: str, message: str) -> Response[str]:
    return Response(status_code=status, content_type=JSON, body=json.dumps({"error": error, "message": message}))


@cache
def _channels() -> frozenset[str]:
    return frozenset(settings().release_channels)


def _plan(channel: str) -> PlaybackPlan | None:
    """The channel's playback plan, or None before its first release."""
    try:
        body = aws.client("s3").get_object(Bucket=settings().media_bucket, Key=plan_key(channel))["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "AccessDenied"}:
            return None
        raise
    return PlaybackPlan.model_validate_json(body)


def _persona(claims: dict[str, Any]) -> str | None:
    """The demo persona the token carries, if it is a well-formed id."""
    try:
        return _Persona(persona=claims.get(PERSONA_CLAIM, "")).persona
    except ValidationError:
        return None


def _claims() -> dict[str, Any] | None:
    """The authorizer's claims of a Cognito access token, or None for anything else."""
    claims: dict[str, Any] = app.current_event.request_context.authorizer.claims or {}
    if claims.get("token_use") != "access" or not claims.get("sub") or not claims.get("iss"):
        return None
    return claims


def _vid(claims: dict[str, Any]) -> str:
    return viewer_id(aws.client("kms"), key_id=settings().vid_key, issuer=claims["iss"], sub=claims["sub"])


def _forbidden() -> Response[str]:
    return _error(403, "forbidden", "send a Cognito access token with the fast/playback scope")


def _ticket(
    channel: str,
    *,
    vid: Callable[[], str] | None = None,
    persona: str | None = None,
    pm: PrivacyMode | None = None,
    gpp: str | None = None,
    gpp_sid: tuple[int, ...] = (),
) -> Response[str]:
    """A ticket for ``channel``'s stable sibling with these row values, and its URL; ``vid`` computes the signed-in
    viewer's once the channel is known to play."""
    config = settings()
    if channel not in _channels():
        return _error(400, "unknown_channel", f"{channel} is not a monetized channel")
    plan = _plan(channel)
    if plan is None:
        return _error(503, "not_released", f"{channel} has no released playback configuration yet")
    url = plan.playback.get(PREFERRED_OUTPUT) or next(iter(plan.playback.values()))
    cid = str(uuid.uuid7())
    logger.append_keys(cid=cid)
    ticket = mint()
    row = ticket_row(
        ticket=ticket,
        pc=plan.configuration,
        cid=cid,
        now_ms=time.time_ns() // 1_000_000,
        pm=pm,
        vid=vid() if vid is not None else None,
        persona=persona,
        gpp=gpp,
        gpp_sid=gpp_sid,
        redeem_window_ms=config.redeem_window_ms or REDEEM_WINDOW_MS,
    )
    aws.table(config.identity_table).put_item(Item=row.item(), ConditionExpression="attribute_not_exists(pk)")
    logger.info("ticket minted", extra={"channel": channel, "pc": plan.configuration, "guest": row.vid is None})
    body = {"url": f"{url}?ads.t={ticket}", "cid": cid, "expires_in": (row.redeem_by - row.iat) // 1000}
    return Response(status_code=200, content_type=JSON, body=json.dumps(body))


@app.post("/playback")
def playback() -> Response[str]:
    settings().require("media_bucket", "identity_table", "vid_key", "release_channels")
    if (claims := _claims()) is None:
        return _forbidden()
    try:
        request = PlaybackRequest.model_validate_json(app.current_event.body or "")
    except ValidationError:
        return _error(400, "invalid_request", 'the body is {"channel": "<channel name>"}')
    return _ticket(request.channel, vid=lambda: _vid(claims), persona=_persona(claims))


@app.post("/playback/guest")
def guest() -> Response[str]:
    settings().require("media_bucket", "identity_table", "release_channels")
    try:
        request = GuestRequest.model_validate_json(app.current_event.body or "", strict=True)
    except ValidationError:
        return _error(400, "invalid_request", 'the body is {"channel": "<channel name>", "gpp"?, "gpp_sid"?, "gpc"?}')
    headers = {name.lower(): value for name, value in (app.current_event.headers or {}).items()}
    gpc = request.gpc or headers.get(GPC_HEADER, "").strip() == "1"
    return _ticket(request.channel, pm=guest_mode(gpc=gpc), gpp=request.gpp, gpp_sid=request.gpp_sid)


def _consent_body(row: ConsentRecord) -> str:
    return json.dumps(row.model_dump(mode="json", exclude={"pk"}))


@app.get("/consent")
def read_consent() -> Response[str]:
    config = settings()
    config.require("identity_table", "vid_key")
    if (claims := _claims()) is None:
        return _forbidden()
    item = aws.table(config.identity_table).get_item(Key={"pk": consents.key(_vid(claims))}, ConsistentRead=True)
    if "Item" not in item:
        return _error(404, "no_consent", "no consent recorded: the viewer's breaks are SAFE until there is one")
    return Response(status_code=200, content_type=JSON, body=_consent_body(ConsentRecord.model_validate(item["Item"])))


@app.put("/consent")
def write_consent() -> Response[str]:
    config = settings()
    config.require("identity_table", "vid_key")
    if (claims := _claims()) is None:
        return _forbidden()
    try:
        choice = ConsentChoice.model_validate_json(app.current_event.body or "", strict=True)
    except ValidationError:
        return _error(400, "invalid_request", 'the body is {"opt_out": bool, "vppa"?: bool, "gpp"?, "gpp_sid"?}')
    row = consents.record(_vid(claims), choice, now_ms=time.time_ns() // 1_000_000)
    aws.table(config.identity_table).put_item(Item=row.item())
    logger.info("consent recorded", extra={"opt_out": choice.opt_out, "vppa": choice.vppa})
    return Response(status_code=200, content_type=JSON, body=_consent_body(row))


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """API Gateway REST proxy integration of the routes (module docstring) and their CORS preflights."""
    return app.resolve(event, context)
