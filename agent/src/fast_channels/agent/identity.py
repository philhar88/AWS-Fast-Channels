"""Caller identity, taken from the bearer token the Runtime's JWT authorizer already validated.

The Runtime forwards the ``Authorization`` header to the code only because infra allow-lists it
(``RequestHeaderAllowlist``). Signature, issuer, expiry and client are checked by the Runtime before the request
reaches us, so the claims are decoded without verification here. Identity is never taken from the payload: a
payload-supplied actor would let one operator read or poison another operator's memory (_review.md, identity).
"""

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from fast_channels.agent.protocol import AgentRunError


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated principal of one invocation.

    Attributes:
        actor_id: Memory actor id: the JWT ``sub`` for operators, the ``client_id`` for machine clients.
        client_id: Cognito app client that obtained the token.
        groups: ``cognito:groups`` claim (operator RBAC groups).
        machine: True for client-credentials tokens (Autopilot), which carry no ``username``.
        authorization: The original ``Authorization`` header, forwarded verbatim to the Gateway.
    """

    actor_id: str
    client_id: str | None
    groups: tuple[str, ...]
    machine: bool
    authorization: str

    @classmethod
    def from_headers(cls, headers: Mapping[str, str] | None) -> Self:
        """Build the caller from request headers; raises ``AgentRunError("UNAUTHENTICATED")``."""
        authorization = next((value for name, value in (headers or {}).items() if name.lower() == "authorization"), "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AgentRunError("UNAUTHENTICATED", "A bearer access token is required.")
        claims = _claims(token)
        client_id = claims.get("client_id")
        machine = "username" not in claims
        actor_id = client_id if machine else claims.get("sub")
        if not isinstance(actor_id, str) or not actor_id:
            raise AgentRunError("UNAUTHENTICATED", "The access token has no subject.")
        return cls(
            actor_id=actor_id,
            client_id=client_id if isinstance(client_id, str) else None,
            groups=tuple(claims.get("cognito:groups") or ()),
            machine=machine,
            authorization=authorization,
        )


def _claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, binascii.Error) as error:
        raise AgentRunError("UNAUTHENTICATED", "The access token is not a JWT.") from error
    if not isinstance(claims, dict):
        raise AgentRunError("UNAUTHENTICATED", "The access token is not a JWT.")
    return claims
