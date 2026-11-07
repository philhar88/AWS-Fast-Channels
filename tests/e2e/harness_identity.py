"""Viewer sign-in and playback tickets for the e2e tests (``modules.identity``, ADR-0018).

Any e2e that plays as a signed-in viewer uses these:

    token = sign_in(stage, "dana")             # a persona seeded by scripts/seed_personas.py
    answer = mint(stage, token, "fastm-demo")  # POST /playback: {"url", "cid", "expires_in"}
    ticket_of(answer["url"])                   # the 22-character ads.t
    guest(stage, "fastm-demo", gpc=True)       # POST /playback/guest, no token
    record_consent(stage, token, opt_out=False)  # PUT /consent: without one the viewer's breaks are SAFE

``sign_in`` reads the persona's password from the Secrets Manager secret ``<prefix>/identity/demo-users`` and the app
client from ``/<prefix>/identity/client-id``, and signs in with ``USER_PASSWORD_AUTH`` as the viewer app does. A test
that creates its own user passes its email and password instead. ``ticket_of`` and ``claims`` are pure
(unit-tested in ``test_harness_identity.py``).
"""

import base64
import json
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
from harness import Player, wait_for
from harness_pods import Viewer

from fast_channels.core.identity.consent import key as consent_key
from fast_channels.core.identity.names import PERSONA_ATTRIBUTE, Parameter, demo_users_secret, parameter_name
from fast_channels.core.identity.ticket import TICKET
from fast_channels.core.identity.vid import viewer_id

if TYPE_CHECKING:
    from _stack import Stage

TICKET_PARAMETER: Final = "ads.t"
TIMEOUT: Final = httpx.Timeout(15.0)


def parameter(stage: Stage, which: Parameter) -> str:
    """The stage's identity SSM parameter ``which``."""
    value: str = stage.session.client("ssm").get_parameter(Name=parameter_name(stage.prefix, which))["Parameter"][
        "Value"
    ]
    return value


def credentials(stage: Stage, persona: str) -> tuple[str, str]:
    """The seeded persona's email and password (``scripts/seed_personas.py``)."""
    secret = stage.session.client("secretsmanager").get_secret_value(SecretId=demo_users_secret(stage.prefix))
    users: dict[str, dict[str, str]] = json.loads(secret["SecretString"])
    if persona not in users:
        raise LookupError(f"persona {persona!r} is not seeded: run scripts/seed_personas.py --prefix {stage.prefix}")
    return users[persona]["email"], users[persona]["password"]


def tokens(stage: Stage, email: str, password: str) -> dict[str, str]:
    """The viewer's Cognito tokens: ``AccessToken``, ``IdToken`` and ``RefreshToken``."""
    answer = stage.session.client("cognito-idp").initiate_auth(
        ClientId=parameter(stage, Parameter.CLIENT_ID),
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    return {name: value for name, value in answer["AuthenticationResult"].items() if isinstance(value, str)}


@dataclass(frozen=True)
class SignedIn:
    """A viewer of the pool, signed in: their ``sub`` (for computing their ``vid``) and tokens."""

    email: str
    sub: str
    access_token: str
    id_token: str


@contextmanager
def viewer(stage: Stage, email: str, *, persona: str) -> Iterator[SignedIn]:
    """A pool user of the test's own (``email``, ``custom:persona``), signed in with a password as the viewer app
    does, and deleted afterwards with any consent they recorded. No email is sent."""
    cognito = stage.session.client("cognito-idp")
    pool, password = parameter(stage, Parameter.USER_POOL_ID), f"{secrets.token_urlsafe(18)}aA1!"
    cognito.admin_create_user(
        UserPoolId=pool,
        Username=email,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
            {"Name": f"custom:{PERSONA_ATTRIBUTE}", "Value": persona},
        ],
        MessageAction="SUPPRESS",
    )
    try:
        cognito.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
        user = cognito.admin_get_user(UserPoolId=pool, Username=email)
        sub = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")
        signed_in = tokens(stage, email, password)
        try:
            yield SignedIn(email, sub, signed_in["AccessToken"], signed_in["IdToken"])
        finally:
            vid = viewer_id(
                stage.session.client("kms"),
                key_id=parameter(stage, Parameter.VID_KEY),
                issuer=parameter(stage, Parameter.ISSUER),
                sub=sub,
            )
            table = stage.session.resource("dynamodb").Table(parameter(stage, Parameter.TABLE))
            table.delete_item(Key={"pk": consent_key(vid)})
    finally:
        cognito.admin_delete_user(UserPoolId=pool, Username=email)


def sign_in(stage: Stage, persona: str) -> str:
    """A seeded persona's access token, the one ``POST /playback`` takes."""
    return tokens(stage, *credentials(stage, persona))["AccessToken"]


def mint(stage: Stage, token: str, channel: str) -> dict[str, Any]:
    """``POST /playback`` for ``channel``: ``{"url", "cid", "expires_in"}``. Raises on any answer but 200."""
    response = httpx.post(
        parameter(stage, Parameter.PLAYBACK_URL),
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": channel},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    return answer


def guest(
    stage: Stage, channel: str, *, gpp: str | None = None, gpp_sid: tuple[int, ...] = (), gpc: bool = False
) -> dict[str, Any]:
    """``POST /playback/guest`` for ``channel``: ``{"url", "cid", "expires_in"}``. Raises on any answer but 200."""
    body: dict[str, Any] = {"channel": channel, **({"gpp": gpp, "gpp_sid": list(gpp_sid)} if gpp else {})}
    response = httpx.post(
        f"{parameter(stage, Parameter.PLAYBACK_URL)}/guest", json=body | ({"gpc": True} if gpc else {}), timeout=TIMEOUT
    )
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    return answer


def consent_url(stage: Stage) -> str:
    """``GET`` and ``PUT`` here read and write the signed-in viewer's consent (beside ``/playback``)."""
    return parameter(stage, Parameter.PLAYBACK_URL).removesuffix("playback") + "consent"


def record_consent(stage: Stage, token: str, *, opt_out: bool, vppa: bool = False) -> dict[str, Any]:
    """``PUT /consent``: the viewer's choice, as stored. Raises on any answer but 200."""
    response = httpx.put(
        consent_url(stage),
        headers={"Authorization": f"Bearer {token}"},
        json={"opt_out": opt_out, "vppa": vppa},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    return answer


def ticket_of(url: str) -> str:
    """The ticket of a minted playback URL (a ``ValueError`` if it has none, several or a malformed one)."""
    values = parse_qs(urlsplit(url).query).get(TICKET_PARAMETER, [])
    if len(values) != 1 or not TICKET.fullmatch(values[0]):
        raise ValueError(f"no single valid {TICKET_PARAMETER} in {url!r}")
    return values[0]


def claims(token: str) -> dict[str, Any]:
    """A JWT's payload, unverified: API Gateway verifies tokens; tests only read what Cognito put in."""
    payload = token.split(".")[1]
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return decoded


SERVING_TIMEOUT_S: Final = 300
"""How long a just-started channel may take to serve its playlist (its origin answers 404 until then)."""
JOIN_ATTEMPTS: Final = 3


def wait_until_serving(http: httpx.Client, url: str) -> None:
    """Until the channel's plain playback URL answers its multivariant playlist (a guest session)."""

    def serving() -> bool | None:
        return http.get(url).status_code == httpx.codes.OK or None

    wait_for(serving, timeout=SERVING_TIMEOUT_S, interval=5, what=f"the channel to serve {url}")


def join(stage: Stage, http: httpx.Client, token: str, channel: str) -> tuple[Viewer, dict[str, Any]]:
    """A signed-in viewer's session, as the viewer app starts one, and the ``POST /playback`` answer it played: a
    ticket minted for each attempt, never a ticket retried. A ticket binds the first session that redeems it, and
    MediaTailor creates (and redeems) the session even when it answers the player an error (a just-started channel's
    404), so a retry with the same ticket would be a replay (docs/identity.md)."""
    failures = []
    for _ in range(JOIN_ATTEMPTS):
        answer = mint(stage, token, channel)
        response = http.get(answer["url"])  # minted and joined at once: redeemed within 120 s
        if response.status_code == httpx.codes.OK:
            return Viewer.of(Player(client=http), response), answer
        failures.append(response.status_code)
        time.sleep(5)
    raise AssertionError(f"no session after {JOIN_ATTEMPTS} tickets: {failures}")
