"""The viewer pool's pre-token-generation trigger (event version 2): the access token carries what ``POST /playback``
needs.

- **The scope ``fast/playback``.** Password and SRP sign-in grant no custom scope, so the trigger adds it to every
  access token of the pool. ``POST /playback`` requires it, and ID tokens never carry scopes, so they are refused.
- **The claim ``fast:persona``**, the viewer's ``custom:persona`` (a demo persona id). The mint copies it onto the
  ticket row. An access token carries no user attribute otherwise.

It never changes the ID token, and it adds nothing for a user without a well-formed persona but the scope. The
access-token customisation needs the pool's Essentials (or Plus) feature plan.
"""

import re
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger

from fast_channels.core.identity.names import PERSONA_ATTRIBUTE, PERSONA_CLAIM, PLAYBACK_SCOPE

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
PERSONA: Final = re.compile(r"[a-z][a-z0-9-]{0,31}")


def overrides(user_attributes: dict[str, str]) -> dict[str, Any]:
    """The ``claimsAndScopeOverrideDetails`` of a user with ``user_attributes``."""
    access: dict[str, Any] = {"scopesToAdd": [PLAYBACK_SCOPE]}
    persona = user_attributes.get(f"custom:{PERSONA_ATTRIBUTE}", "")
    if PERSONA.fullmatch(persona):
        access["claimsToAddOrOverride"] = {PERSONA_CLAIM: persona}
    return {"accessTokenGeneration": access}


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Cognito ``PreTokenGeneration_*`` (version 2): the event, with the response's overrides set."""
    attributes: dict[str, str] = event.get("request", {}).get("userAttributes", {})
    event.setdefault("response", {})["claimsAndScopeOverrideDetails"] = overrides(attributes)
    logger.info("access token customised", extra={"trigger": event.get("triggerSource")})
    return event
