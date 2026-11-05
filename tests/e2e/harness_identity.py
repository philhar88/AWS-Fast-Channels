"""Viewer sign-in and playback tickets for the e2e tests (``modules.identity``, ADR-0018).

Any e2e that plays as a signed-in viewer uses these:

    token = sign_in(stage, "dana")             # a persona seeded by scripts/seed_personas.py
    answer = mint(stage, token, "fastm-demo")  # POST /playback: {"url", "cid", "expires_in"}
    ticket_of(answer["url"])                   # the 22-character ads.t

``sign_in`` reads the persona's password from the Secrets Manager secret ``<prefix>/identity/demo-users`` and the app
client from ``/<prefix>/identity/client-id``, and signs in with ``USER_PASSWORD_AUTH`` as the viewer app does. A test
that creates its own user passes its email and password instead. ``ticket_of`` and ``claims`` are pure
(unit-tested in ``test_harness_identity.py``).
"""

import base64
import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx

from fast_channels.core.identity.names import PERSONA_ATTRIBUTE, Parameter, demo_users_secret, parameter_name
from fast_channels.core.identity.ticket import TICKET

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
    does, and deleted afterwards. No email is sent."""
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
        yield SignedIn(email, sub, signed_in["AccessToken"], signed_in["IdToken"])
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
