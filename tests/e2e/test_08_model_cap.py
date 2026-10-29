"""The scheduler agent's daily spend cap (``modules.models``): past it, runs are refused with ``MODEL_CAP_REACHED``
and the alert topic is told once.

Runs only on a stage whose ``scheduler.plan`` registry entry has a tiny cap (at most 5 US cents), because it spends
the stage's agent budget for the rest of the UTC day; deploy the e2e stage with
``-c 'fast-channels.modules.models={"enabled": true, "tasks": {"scheduler.plan": {"daily_cap_usd": 0.01}}}'`` and
the machine client (see ``test_05_agent``). It runs last for the same reason."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from _stack import Plane, Stage
from harness import AGENT_SCOPE, Player, ask_agent, machine_token

pytestmark = pytest.mark.e2e

TINY_CAP = Decimal("0.05")
MAX_RUNS = 12
PROMPT = "List the channels."
CAP_REACHED = "MODEL_CAP_REACHED"


def _entry(stage: Stage) -> dict[str, object] | None:
    ssm = stage.session.client("ssm", region_name=stage.region)
    try:
        value = ssm.get_parameter(Name=f"/{stage.prefix}/models/tasks/scheduler.plan")["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None
    entry: dict[str, object] = json.loads(value)
    return entry


def test_runs_past_the_cap_are_refused_and_notified_once(stage: Stage, player: Player) -> None:
    entry = _entry(stage)
    if entry is None:
        pytest.skip("no model registry: deploy with modules.models.enabled")
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

    outcomes: list[str] = []
    for _ in range(MAX_RUNS):
        events = ask_agent(player.client, invoke_url=invoke_url, token=token, prompt=PROMPT)
        errors = [event for event in events if event.get("type") == "RUN_ERROR"]
        outcomes.append(str(errors[0].get("code")) if errors else "finished")
        if errors:
            break
    assert outcomes[-1] == CAP_REACHED, outcomes
    assert all(outcome == "finished" for outcome in outcomes[:-1]), outcomes
    again = ask_agent(player.client, invoke_url=invoke_url, token=token, prompt=PROMPT)
    assert [e.get("code") for e in again if e.get("type") == "RUN_ERROR"] == [CAP_REACHED]  # still refused

    day = datetime.now(UTC).strftime("%Y-%m-%d")
    dynamodb = stage.session.client("dynamodb", region_name=stage.region)
    item = dynamodb.get_item(
        TableName=f"{stage.prefix}-models", Key={"pk": {"S": f"COUNTER#scheduler.plan#{day}"}}, ConsistentRead=True
    )["Item"]
    assert Decimal(item["spent_usd"]["N"]) >= Decimal(str(cap)), item
    assert "notified_at" in item, item

    runtime_id = stage.output(Plane.OPERATOR, "RuntimeArn").rsplit("/", 1)[-1]
    logs = stage.session.client("logs", region_name=stage.region)
    group = f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT"
    notices: list[str] = []
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:  # runtime logs arrive with a delay
        page = logs.filter_log_events(
            logGroupName=group, startTime=started_ms - 60_000, filterPattern='"model spend cap reached"'
        )
        notices = [event["message"] for event in page["events"]]
        if notices:
            time.sleep(20)  # let any duplicate arrive too
            page = logs.filter_log_events(
                logGroupName=group, startTime=started_ms - 60_000, filterPattern='"model spend cap reached"'
            )
            notices = [event["message"] for event in page["events"]]
            break
        time.sleep(15)
    assert len(notices) == 1, notices  # told once, although two runs were refused
