"""The scheduling agent over AG-UI, called as the autopilot calls it: a client-credentials token of the autopilot app
client, one ``RunAgentInput`` to the AgentCore Runtime, Server-Sent Events back (CONTRACTS §G)."""

import pytest
from _stack import Plane, Stage
from agui import tool_calls
from harness import AGENT_SCOPE, Player, ask_agent, machine_token, wait_for

pytestmark = pytest.mark.e2e

PROMPT = "List the channels."
EXPECTED_TOOL = "channels___list_channels"


def test_agent_lists_the_channels(stage: Stage, player: Player) -> None:
    pool, client_id = (stage.output(Plane.OPERATOR, key) for key in ("UserPoolId", "AutopilotClientId"))
    cognito = stage.session.client("cognito-idp", region_name=stage.region)
    secret = cognito.describe_user_pool_client(UserPoolId=pool, ClientId=client_id)["UserPoolClient"]["ClientSecret"]
    token = machine_token(
        player.client,
        domain=stage.output(Plane.OPERATOR, "CognitoDomain"),
        client_id=client_id,
        client_secret=secret,
        scope=AGENT_SCOPE,
    )
    invoke_url = stage.output(Plane.OPERATOR, "RuntimeInvokeUrl")
    last: list[dict[str, object]] = []

    def run() -> list[dict[str, object]] | None:
        # Each attempt is a new conversation, i.e. a new runtime session: right after a deployment the first one
        # can start before the Gateway's policies list any tool.
        last[:] = ask_agent(player.client, invoke_url=invoke_url, token=token, prompt=PROMPT)
        return list(last) if EXPECTED_TOOL in tool_calls(last) else None

    def summary() -> object:
        return [(event.get("type"), event.get("toolCallName") or event.get("message")) for event in last][-12:]

    events = wait_for(run, timeout=420, interval=60, what=f"a {EXPECTED_TOOL} tool call", state=summary)
    types = [event.get("type") for event in events]
    assert "RUN_ERROR" not in types, summary()
    assert types[0] == "RUN_STARTED" and "RUN_FINISHED" in types, types
