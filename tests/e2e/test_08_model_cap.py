"""The scheduler agent's daily spend cap (``modules.models``): past it, runs are refused with ``MODEL_CAP_REACHED``
and the alert topic is told once.

Runs only on a stage whose ``scheduler.plan`` registry entry has a tiny cap (at most 5 US cents), because it spends
the stage's agent budget for the rest of the UTC day; deploy the e2e stage with
``-c 'fast-channels.modules.models.tasks={"scheduler.plan": {"daily_cap_usd": 0.01}}'`` and the machine client (see
``test_05_agent``). It runs last for the same reason.

A cached agent turn costs a few hundredths of a cent, so turns alone take long to reach even a cent. After one turn
has been counted, the test lowers the registry entry's cap below the day's spend (an operator's runtime edit of the
entry) and puts the entry back as it was at the end."""

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from _stack import Plane, Stage
from harness import AGENT_SCOPE, Player, ask_agent, machine_token

pytestmark = pytest.mark.e2e

TINY_CAP = Decimal("0.05")
PROMPT = "List the channels."
CAP_REACHED = "MODEL_CAP_REACHED"
OTEL_STREAM = "otel-rt-logs"
"""The runtime writes each record twice, to this stream and to the session's stdout stream: count it once."""


@pytest.fixture
def entry(stage: Stage) -> Iterator[dict[str, Any]]:
    """``scheduler.plan``'s registry entry, put back exactly as it was after the test."""
    ssm = stage.session.client("ssm", region_name=stage.region)
    name = f"/{stage.prefix}/models/tasks/scheduler.plan"
    try:
        original = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        pytest.skip("no model registry: deploy with modules.models.enabled")
    document: dict[str, Any] = json.loads(original)
    document["_name"] = name
    try:
        yield document
    finally:
        ssm.put_parameter(Name=name, Type="String", Overwrite=True, Value=original)


def _counter(stage: Stage) -> dict[str, Any]:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    dynamodb = stage.session.client("dynamodb", region_name=stage.region)
    item: dict[str, Any] = dynamodb.get_item(
        TableName=f"{stage.prefix}-models", Key={"pk": {"S": f"COUNTER#scheduler.plan#{day}"}}, ConsistentRead=True
    ).get("Item", {})
    return item


def _errors(events: list[dict[str, Any]]) -> list[object]:
    return [event.get("code") for event in events if event.get("type") == "RUN_ERROR"]


def test_runs_past_the_cap_are_refused_and_notified_once(stage: Stage, player: Player, entry: dict[str, Any]) -> None:
    cap = entry.get("daily_cap_usd")
    if cap is None or Decimal(str(cap)) > TINY_CAP:
        pytest.skip(f"scheduler.plan's daily cap is {cap}: deploy with a cap of at most {TINY_CAP} to test refusal")
    if "AutopilotClientId" not in stage.outputs(Plane.OPERATOR):
        pytest.skip("no machine client: deploy with -c fast-channels.agent.machine_client=true")
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
    started_ms = int(time.time() * 1000)

    first = ask_agent(player.client, invoke_url=invoke_url, token=token, prompt=PROMPT)
    assert _errors(first) == [] and first[-1].get("type") == "RUN_FINISHED", _errors(first)
    spent = Decimal(_counter(stage)["spent_usd"]["N"])
    assert spent > 0  # the turn's calls were counted at list prices
    ssm = stage.session.client("ssm", region_name=stage.region)
    lowered = {key: value for key, value in entry.items() if key != "_name"} | {"daily_cap_usd": float(spent / 2)}
    ssm.put_parameter(Name=entry["_name"], Type="String", Overwrite=True, Value=json.dumps(lowered))
    time.sleep(5)

    refused = [ask_agent(player.client, invoke_url=invoke_url, token=token, prompt=PROMPT) for _ in range(2)]

    assert [_errors(events) for events in refused] == [[CAP_REACHED], [CAP_REACHED]]
    item = _counter(stage)
    assert Decimal(item["spent_usd"]["N"]) == spent  # refused runs make no model call
    assert "notified_at" in item, item
    runtime_id = stage.output(Plane.OPERATOR, "RuntimeArn").rsplit("/", 1)[-1]
    logs = stage.session.client("logs", region_name=stage.region)

    def notices() -> list[str]:
        page = logs.filter_log_events(
            logGroupName=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
            logStreamNames=[OTEL_STREAM],
            startTime=started_ms - 60_000,
            filterPattern='"model spend cap reached"',
        )
        return [event["message"] for event in page["events"]]

    deadline = time.monotonic() + 180
    while not notices() and time.monotonic() < deadline:  # runtime logs arrive with a delay
        time.sleep(15)
    time.sleep(20)  # let any duplicate arrive too
    assert len(notices()) == 1, notices()  # told once, although two runs were refused
