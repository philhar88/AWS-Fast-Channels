"""``POST /playback``: a signed-in viewer's single-use playback ticket (DESIGN-v2.2 §5.1; ADR-0018).

API Gateway REST runs the viewer pool's Cognito authorizer first: only an access token carrying the scope
``fast/playback`` gets here (an ID token never carries it). The function computes everything server-side:

- the ``vid``, a KMS HMAC of the token's issuer and subject (``core.identity.vid``);
- the privacy mode: ``PSEUDONYMOUS`` for every signed-in viewer until a consent store exists;
- the demo persona, from the access token's ``fast:persona`` claim (the pre-token-generation trigger copies
  ``custom:persona``);
- the playback configuration: the channel's stable sibling, from its playback plan (``plan/playback/<channel>.json``,
  which the Release Controller rewrites at every promotion and rollback).

It writes the row ``T#<t>`` (``core.identity.ticket``) and answers the plan's CDN playback URL with ``?ads.t=<t>``.
The ticket is the only viewer value in the URL. A player opens it within the redeem window, in one session; every
new play asks for a new ticket. Anything else (an unknown channel, a channel never released) is an error the player
answers by playing the plan's anonymous URL.

Nothing about the viewer is logged but the correlation id ``cid``, the channel and the configuration: never the
ticket, the ``vid`` or the token's claims.
"""

import json
import time
import uuid
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, CORSConfig, Response
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fast_channels.core.identity.names import PERSONA_CLAIM
from fast_channels.core.identity.ticket import REDEEM_WINDOW_MS, PrivacyMode, mint, ticket_row
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
JSON: Final = "application/json"
PREFERRED_OUTPUT: Final = "hls"
PERSONA: Final = r"^[a-z][a-z0-9-]{0,31}$"


class PlaybackRequest(BaseModel):
    """The body of ``POST /playback``."""

    model_config = ConfigDict(extra="forbid")

    channel: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


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


@app.post("/playback")
def playback() -> Response[str]:
    config = settings()
    config.require("media_bucket", "identity_table", "vid_key", "release_channels")
    claims: dict[str, Any] = app.current_event.request_context.authorizer.claims or {}
    if claims.get("token_use") != "access" or not claims.get("sub") or not claims.get("iss"):
        return _error(403, "forbidden", "send a Cognito access token with the fast/playback scope")
    try:
        request = PlaybackRequest.model_validate_json(app.current_event.body or "")
    except ValidationError:
        return _error(400, "invalid_request", 'the body is {"channel": "<channel name>"}')
    if request.channel not in _channels():
        return _error(400, "unknown_channel", f"{request.channel} is not a monetized channel")
    plan = _plan(request.channel)
    if plan is None:
        return _error(503, "not_released", f"{request.channel} has no released playback configuration yet")
    url = plan.playback.get(PREFERRED_OUTPUT) or next(iter(plan.playback.values()))
    cid = str(uuid.uuid7())
    logger.append_keys(cid=cid)
    ticket = mint()
    row = ticket_row(
        ticket=ticket,
        pc=plan.configuration,
        cid=cid,
        now_ms=time.time_ns() // 1_000_000,
        pm=PrivacyMode.PSEUDONYMOUS,
        vid=viewer_id(aws.client("kms"), key_id=config.vid_key, issuer=claims["iss"], sub=claims["sub"]),
        persona=_persona(claims),
        redeem_window_ms=config.redeem_window_ms or REDEEM_WINDOW_MS,
    )
    aws.table(config.identity_table).put_item(Item=row.item(), ConditionExpression="attribute_not_exists(pk)")
    logger.info("ticket minted", extra={"channel": request.channel, "pc": plan.configuration})
    body = {"url": f"{url}?ads.t={ticket}", "cid": cid, "expires_in": (row.redeem_by - row.iat) // 1000}
    return Response(status_code=200, content_type=JSON, body=json.dumps(body))


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """API Gateway REST proxy integration of ``POST /playback`` (and its CORS preflight)."""
    return app.resolve(event, context)
