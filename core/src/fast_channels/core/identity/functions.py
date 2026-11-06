"""The identity module's PRE_SESSION tree (DESIGN-v2.2 §5.3, ADR-0018): the ticket redeem, contributed to the release
bundle as a :class:`~fast_channels.core.monetization.bundle.BundleContribution`.

On every ticketed sibling (a general sibling of a monetized channel that is neither the lab channel nor child-directed,
on the Prebid Server route), the PRE_SESSION root executor first runs ``<p>_<sibling>_ps_redeem``: an ``HTTP_REQUEST``
POST of ``{t, sid, pc}`` to the identity API's ``POST /redeem``, which binds the ticket to ``session.uuid`` with one
conditional DynamoDB update (``core.identity.ticket.REDEEM_CONDITION``). The session controls run after it, so
nothing the redeem answers can change them.

- **The ticket** is ``player_params.t`` (the edge rebuild keeps only a single valid ``ads.t``). The body takes at most
  22 URL-safe characters of it whatever a direct caller sends, so it stays valid JSON.
- **The configuration** is a constant: no documented Functions variable names the calling playback configuration, so
  the redeem embeds it and is one function per sibling.
- **No ticket, no call.** The step's ``RunCondition`` skips it for a session without ``ads.t``.
- **Never cached** (cache scope ``none``): the call is single-use and binds this one session.
- **It never fails the session.** A 409 (a replay, an expired or unknown ticket, another configuration) or a 400
  leaves the session anonymous; the redeem writes ``temp.redeem`` only, which the root does not commit. The Pod
  Decision Service decides each break from the ticket's row, and self-redeems a fresh ticket if the call never
  arrived (``core.identity.binding``).
"""

from collections.abc import Iterable
from typing import Final

from fast_channels.core.monetization.bundle import HookTree, SiblingContext, Step
from fast_channels.core.monetization.functions import (
    CacheScope,
    Function,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    function_stem,
)

NAME: Final = "identity"
REDEEM: Final = "ps_redeem"
"""The redeem's name in FunctionIds (``<p>_<sibling>_ps_redeem_rNNNN``)."""
REQUEST_TIMEOUT_MS: Final = 800
"""The redeem's timeout: 55-81 ms measured, the hook p99 217 ms with it (P0-05)."""
EXECUTOR_BUDGET_MS: Final = 800
"""The redeem's share of the PRE_SESSION root timeout; with the session controls' 400 ms the root allows 1,200 ms
(DESIGN §5.3)."""
TICKET_PRESENT: Final = "{% $exists(player_params.t) and $string(player_params.t) != '' %}"
"""RunCondition: a session without a ticket makes no call."""
CACHE_REASON: Final = (
    "A redeem binds a single-use ticket to the one session that calls it: every call must reach the identity API, "
    "and no session may reuse another's answer."
)
"""Why the redeem is never cached (validator R-F8)."""
OUTPUT: Final = {"temp.redeem": "{% response.statusCode = 200 ? 'bound' : 'anonymous' %}"}
"""What the redeem writes: a temporary the root does not commit (readable in a lab sibling's hook events)."""


def redeem_body(configuration: str) -> str:
    """The redeem's ``Body`` expression for the sibling ``configuration``: ``{t, sid, pc}`` as JSON, the ticket cut to
    at most 22 URL-safe characters.

    Raises:
        ValueError: ``configuration`` holds a character a JSONata string literal cannot.
    """
    if any(char in configuration for char in "'\\"):
        raise ValueError(f"configuration {configuration!r} may not contain a quote or a backslash")
    return (
        "{% $string({'t': $substring($replace($string(player_params.t), /[^A-Za-z0-9_-]/, ''), 0, 22), "
        f"'sid': session.uuid, 'pc': '{configuration}'}}) %}}"
    )


def ps_redeem(prefix: str, *, sibling: str, redeem_url: str) -> Function:
    """``<p>_<sibling>_ps_redeem``: the sibling's ticket redeem (module docstring)."""
    return Function(
        function_id=function_stem(prefix, REDEEM, sibling=sibling),
        function_type=FunctionType.HTTP_REQUEST,
        description="Ticket redeem: binds the playback ticket to this session (identity API, single use).",
        http_request_configuration=HttpRequestConfiguration(
            method_type="POST",
            url=redeem_url,
            headers={"Content-Type": "application/json"},
            body=redeem_body(sibling),
            request_timeout_milliseconds=REQUEST_TIMEOUT_MS,
            cache_scope=CacheScope.NONE,
            cache_reason=CACHE_REASON,
            output=dict(OUTPUT),
        ),
    )


class IdentityContribution:
    """The PRE_SESSION ticket redeem on every ticketed sibling (module docstring).

    Args:
        redeem_url: the identity API's ``POST /redeem`` (``/<prefix>/identity/redeem-url``).
    """

    name = NAME

    def __init__(self, *, redeem_url: str) -> None:
        if not redeem_url.startswith("https://"):
            raise ValueError(f"the redeem URL must be https://, got {redeem_url!r}")
        self.redeem_url = redeem_url

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        if not sibling.ticketed:
            return
        yield HookTree(
            hook=Hook.PRE_SESSION,
            steps=(
                Step(
                    function=ps_redeem(sibling.prefix, sibling=sibling.name, redeem_url=self.redeem_url),
                    run_condition=TICKET_PRESENT,
                ),
            ),
            budget_ms=EXECUTOR_BUDGET_MS,
        )
