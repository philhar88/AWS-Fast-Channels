"""Names of the identity module's resources, shared by the ``Identity`` stack, its functions, the Release Controller
and the scripts, so every side agrees.

Other stacks and the scripts read what the ``Identity`` stack knows through the SSM parameters below
(``/<prefix>/identity/<name>``), never through a cross-stack reference.
"""

from enum import StrEnum
from typing import Final

MODULE: Final = "identity"
RESOURCE_SERVER: Final = "fast"
PLAYBACK_SCOPE_NAME: Final = "playback"
PLAYBACK_SCOPE: Final = f"{RESOURCE_SERVER}/{PLAYBACK_SCOPE_NAME}"
"""The OAuth scope ``POST /playback`` requires of an access token. ID tokens never carry it, so they are refused: they
also carry the viewer's email."""
PERSONA_ATTRIBUTE: Final = "persona"
"""The viewer pool's custom attribute ``custom:persona``: the demo persona id of ``contracts/personas.json``."""
PERSONA_CLAIM: Final = "fast:persona"
"""The access-token claim the pre-token-generation trigger copies ``custom:persona`` into."""
DEMO_USERS_SECRET: Final = "identity/demo-users"
"""Secrets Manager name (after ``<prefix>/``) of the demo viewers' sign-in: ``{"<persona>": {"email", "password"}}``."""


class Parameter(StrEnum):
    """SSM parameters ``/<prefix>/identity/<name>`` the ``Identity`` stack writes."""

    USER_POOL_ID = "user-pool-id"
    CLIENT_ID = "client-id"
    """The viewer app client: public, no secret, SRP and password sign-in."""
    E2E_CLIENT_ID = "e2e-client-id"
    """The client the e2e tests sign in with (the viewer app client)."""
    ISSUER = "issuer"
    """``https://cognito-idp.<region>.amazonaws.com/<pool id>``: the ``iss`` of the pool's tokens."""
    VID_KEY = "vid-key"
    """ARN of the KMS HMAC key the ``vid`` is computed with."""
    TABLE = "table"
    """The ticket table."""
    PLAYBACK_URL = "playback-url"
    """``POST`` here mints a ticket (Cognito access token with :data:`PLAYBACK_SCOPE`)."""
    REDEEM_URL = "redeem-url"
    """``POST`` here binds a ticket to a session: the PRE_SESSION Function's request."""


def parameter_name(prefix: str, parameter: Parameter) -> str:
    """``/<prefix>/identity/<parameter>``."""
    return f"/{prefix}/{MODULE}/{parameter.value}"


def table_name(prefix: str) -> str:
    """The ticket table: one row ``T#<t>`` per minted ticket."""
    return f"{prefix}-identity"


def user_pool_name(prefix: str) -> str:
    """The viewer pool."""
    return f"{prefix}-viewers"


def vid_key_alias(prefix: str) -> str:
    """``alias/<prefix>-identity-vid``: the KMS HMAC key of :func:`fast_channels.core.identity.vid.viewer_id`."""
    return f"alias/{prefix}-identity-vid"


def api_name(prefix: str) -> str:
    """The REST API of ``POST /playback`` and ``POST /redeem``."""
    return f"{prefix}-identity"


def demo_users_secret(prefix: str) -> str:
    """``<prefix>/identity/demo-users``."""
    return f"{prefix}/{DEMO_USERS_SECRET}"
