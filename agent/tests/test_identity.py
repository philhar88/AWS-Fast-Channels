import asyncio
from collections.abc import Callable

import pytest

from fast_channels.agent.identity import Caller
from fast_channels.agent.protocol import AgentRunError
from fast_channels.agent.testing import AUTOPILOT_CLAIMS, OPERATOR_CLAIMS, Harness, text, unsigned_jwt


def test_operator_actor_is_the_jwt_subject() -> None:
    token = unsigned_jwt(**OPERATOR_CLAIMS)

    caller = Caller.from_headers({"Authorization": f"Bearer {token}"})

    assert caller.actor_id == "user-123"
    assert caller.client_id == "console"
    assert caller.groups == ("fast-operators",)
    assert caller.machine is False
    assert caller.authorization == f"Bearer {token}"


def test_machine_actor_is_the_client_id() -> None:
    caller = Caller.from_headers({"authorization": f"Bearer {unsigned_jwt(**AUTOPILOT_CLAIMS)}"})

    assert caller.actor_id == "autopilot-client"
    assert caller.machine is True
    assert caller.groups == ()


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer a.bm90LWpzb24.c"},
        {"Authorization": "Bearer a.WzFd.c"},  # a JSON array, not claims
    ],
)
def test_rejects_missing_or_malformed_tokens(headers: dict[str, str] | None) -> None:
    with pytest.raises(AgentRunError) as error:
        Caller.from_headers(headers)
    assert error.value.code == "UNAUTHENTICATED"


def test_rejects_tokens_without_subject() -> None:
    with pytest.raises(AgentRunError, match="no subject"):
        Caller.from_headers({"Authorization": f"Bearer {unsigned_jwt(username='x', token_use='access')}"})


def test_actor_comes_from_the_forwarded_token(harness: Callable[..., Harness]) -> None:
    h = harness([text("hello")])

    asyncio.run(h.run("hi", claims={**OPERATOR_CLAIMS, "sub": "from-token"}, actor_id="spoofed"))

    assert h.gateway.callers[0].actor_id == "from-token"
