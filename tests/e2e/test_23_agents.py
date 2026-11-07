"""The agent roster (M9, ADR-0023), end to end: the operations agent restarts an always-on channel that stopped
without a plan, inside its envelope; a second stop within the hour is refused by its cap and escalated; the ledger
records all of it.

Runs only on a stage with the agents module and its first IaC channel always-on (``STAGE=fastciagent``); skipped
otherwise. The module fixture does the billable part once:

1. start the always-on channel as an operator would (the channels tool), and wait until the census has counted it;
2. stop it with Channel Assembly's ``StopChannel`` directly, outside every tool: an unplanned stop. The test first waits
   for an hour with room for both stops, because the agent's cap counts per UTC hour;
3. the census's next run sees no running channel, and its ``Census Changed`` starts the operations agent's runner,
   which finds the channel STOPPED; the agent starts it, the runner approving the call inside the envelope. MTTD is
   from the stop to the runner's first ledger entry, MTTR from the stop to RUNNING;
4. once the census has counted it running again, stop it the same way: the agent's restart is refused by its cap of
   one an hour, and escalated;
5. read the agent's ledger chain ``agents/operations`` and the envelope's ``agents/envelope``.

The channel ends stopped. About 30 minutes: two census intervals (5 minutes each) per stop, and the channel's start.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from _stack import Plane, Stage
from botocore.exceptions import ClientError
from harness import Channel, Tools, wait_for

from fast_channels.core.agents.envelope import ENVELOPE_CHAIN, Envelope, Refusal, Role

pytestmark = pytest.mark.e2e

CENSUS_INTERVAL: Final = timedelta(minutes=5)
NEEDED_IN_THE_HOUR: Final = timedelta(minutes=28)
"""How much of the UTC hour the two stops take at most (the cap's window)."""
REACTION_TIMEOUT_S: Final = 900
"""From a stop to the agent's decision: up to two census runs and the agent's conversation."""
START = "channels___start_channel"


@dataclass
class Run:
    """The two stops and what the agent did about them."""

    stage: Stage
    channel: str
    envelope: Envelope
    first_stop: datetime
    restarted: datetime
    second_stop: datetime
    final_state: str
    entries: list[dict[str, Any]]
    """The agent's ledger entries since the first stop, oldest first."""
    envelope_entries: list[dict[str, Any]]

    def after(self, moment: datetime) -> list[dict[str, Any]]:
        return [e for e in self.entries if datetime.fromisoformat(e["at"]) >= moment]


def runner_envelope(stage: Stage) -> Envelope | None:
    """The operations agent's envelope, as its runner holds it; None without the module."""
    try:
        name = stage.output(Plane.OPERATOR, "OperationsAgentRunner")
    except KeyError, LookupError, ValueError:
        return None
    functions = stage.session.client("lambda", region_name=stage.region)
    variables = functions.get_function_configuration(FunctionName=name)["Environment"]["Variables"]
    return Envelope.model_validate_json(variables["ENVELOPE"])


def channel_state(stage: Stage, name: str) -> str:
    mediatailor = stage.session.client("mediatailor", region_name=stage.region)
    return str(mediatailor.describe_channel(ChannelName=name)["ChannelState"])


def running_count(stage: Stage) -> int | None:
    """The census's last count of running channels (``/<prefix>/ops/running-channels``)."""
    ssm = stage.session.client("ssm", region_name=stage.region)
    try:
        value = ssm.get_parameter(Name=f"/{stage.prefix}/ops/running-channels")["Parameter"]["Value"]
    except ClientError:
        return None
    return int(value)


def ledger(stage: Stage, chain: str, since: datetime) -> list[dict[str, Any]]:
    """The chain's entries at or after ``since``, oldest first."""
    table = stage.session.resource("dynamodb", region_name=stage.region).Table(
        stage.output(Plane.DATA, "LedgerTableName")
    )
    items: list[dict[str, Any]] = []
    query: dict[str, Any] = {"KeyConditionExpression": "pk = :chain", "ExpressionAttributeValues": {":chain": chain}}
    while True:
        page = table.query(ConsistentRead=True, **query)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    entries = [item for item in items if item.get("type") == "entry"]  # not the chain's head and links
    found = [item for item in entries if datetime.fromisoformat(str(item["at"])) >= since]
    return sorted(found, key=lambda item: str(item["sk"]))


def unplanned_stop(stage: Stage, name: str) -> datetime:
    """``StopChannel`` straight to Channel Assembly, as no tool of the stage would; returns when it was asked."""
    at = datetime.now(UTC)
    stage.session.client("mediatailor", region_name=stage.region).stop_channel(ChannelName=name)
    return at


def wait_for_an_hour_with_room() -> None:
    """Until the rest of this UTC hour holds both stops (the cap counts per hour)."""
    now = datetime.now(UTC)
    if now - now.replace(minute=0, second=0, microsecond=0) <= timedelta(hours=1) - NEEDED_IN_THE_HOUR:
        return
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=20)
    print(f"waiting for {next_hour:%H:%M:%S}: the two stops must fall in one UTC hour")
    time.sleep((next_hour - now).total_seconds())


@pytest.fixture(scope="module")
def run(stage: Stage, channel: Channel, tools: Tools) -> Iterator[Run]:
    envelope = runner_envelope(stage)
    if envelope is None:
        pytest.skip(f"stage {stage.prefix} has no operations agent (modules.agents)")
    if channel.name not in envelope.always_on:
        pytest.skip(f"{channel.name} is not always-on in {stage.prefix}'s envelope ({envelope.always_on})")
    try:
        tools("channels___start_channel", channel=channel.name)
        wait_for(
            lambda: True if (running_count(stage) or 0) >= 1 else None,
            timeout=2 * CENSUS_INTERVAL.total_seconds() + 60,
            interval=20,
            what="the census to count the channel running",
        )
        wait_for_an_hour_with_room()
        first = unplanned_stop(stage, channel.name)
        print(f"unplanned stop 1 at {first:%H:%M:%S}")

        def approved() -> list[dict[str, Any]] | None:
            entries = ledger(stage, Role.OPERATIONS.chain_id, first)
            return entries if any(e["action"] == "agent.approve" for e in entries) else None

        wait_for(approved, timeout=REACTION_TIMEOUT_S, interval=20, what="the operations agent's restart")
        wait_for(
            lambda: True if channel_state(stage, channel.name) == "RUNNING" else None,
            timeout=300,
            interval=5,
            what="the channel RUNNING again",
        )
        restarted = datetime.now(UTC)
        print(f"running again at {restarted:%H:%M:%S}")
        wait_for(
            lambda: True if (running_count(stage) or 0) >= 1 else None,
            timeout=2 * CENSUS_INTERVAL.total_seconds() + 60,
            interval=20,
            what="the census to count the restarted channel",
        )
        second = unplanned_stop(stage, channel.name)
        print(f"unplanned stop 2 at {second:%H:%M:%S}")

        def escalated() -> list[dict[str, Any]] | None:
            entries = ledger(stage, Role.OPERATIONS.chain_id, second)
            return entries if any(e["action"] == "agent.escalate" for e in entries) else None

        wait_for(escalated, timeout=REACTION_TIMEOUT_S, interval=20, what="the escalation of the second restart")
        time.sleep(30)  # a restart would have been approved before the escalation; give the state a moment
        final = channel_state(stage, channel.name)
    finally:
        if channel_state(stage, channel.name) != "STOPPED":
            stage.session.client("mediatailor", region_name=stage.region).stop_channel(ChannelName=channel.name)
    found = Run(
        stage=stage,
        channel=channel.name,
        envelope=envelope,
        first_stop=first,
        restarted=restarted,
        second_stop=second,
        final_state=final,
        entries=ledger(stage, Role.OPERATIONS.chain_id, first),
        envelope_entries=ledger(stage, ENVELOPE_CHAIN, datetime(2026, 1, 1, tzinfo=UTC)),
    )
    print(report(found))
    yield found


def report(run: Run) -> str:
    rows = [f"envelope {run.envelope.version}, always-on {run.envelope.always_on}"]
    rows += [
        f"  {str(e['at'])[11:19]} {e['action']:<14} {e['subject']:<28} {(e.get('detail') or '')[:110]}"
        for e in run.entries
    ]
    return "\n".join(rows)


def detail(entry: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(str(entry["detail"]))
    return parsed


def test_the_unplanned_stop_is_detected_and_the_channel_restarted_inside_the_envelope(run: Run) -> None:
    first = [e for e in run.entries if datetime.fromisoformat(e["at"]) < run.second_stop]
    [approve] = [e for e in first if e["action"] == "agent.approve"]
    started = [e for e in first if e["action"] == "agent.tool" and detail(e)["tool"] == START]

    assert detail(approve)["tool"] == START and detail(approve)["channel"] == run.channel
    assert [detail(e)["outcome"] for e in started] == ["done"]
    detected = datetime.fromisoformat(first[0]["at"]) - run.first_stop
    repaired = run.restarted - run.first_stop
    print(f"MTTD {detected.total_seconds():.0f} s, MTTR {repaired.total_seconds():.0f} s")
    assert detected <= CENSUS_INTERVAL * 2 + timedelta(minutes=3)
    assert not [e for e in first if e["action"] in {"agent.refuse", "agent.escalate"}]


def test_every_entry_names_the_agent_and_the_envelope(run: Run) -> None:
    assert run.entries
    assert {e["principal"] for e in run.entries} == {Role.OPERATIONS.principal}
    assert {e["kind"] for e in run.entries} == {"agent"}
    assert {detail(e)["envelope"] for e in run.entries} == {run.envelope.version}
    assert any(detail(e)["version"] == run.envelope.version for e in run.envelope_entries), "envelope.apply"


def test_the_second_restart_within_the_hour_is_refused_and_escalated(run: Run) -> None:
    second = run.after(run.second_stop)
    refused = [e for e in second if e["action"] == "agent.refuse"]
    [escalation] = [e for e in second if e["action"] == "agent.escalate"]

    assert [detail(e)["refusal"] for e in refused] == [Refusal.CAP.value], second
    assert not [e for e in second if e["action"] == "agent.approve"]
    assert any("cap of 1 an hour" in reason for reason in detail(escalation)["refused"])
    assert run.final_state == "STOPPED", "a refused restart leaves the channel to a human"
