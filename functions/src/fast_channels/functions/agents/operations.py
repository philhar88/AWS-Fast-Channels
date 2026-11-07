"""The operations agent's triggers (M9): an always-on channel found stopped (runbook R12) becomes a run.

Two triggers start the operations agent's runner: the channel census's ``Census Changed`` to zero running channels
(the stage's bus, at most one census interval after the stop) and the probe canary's off-air alarm (runbook R12's
trigger; the census stops the canary when the last channel stops, so it rarely fires for a stop). Either way the
runner first reads each always-on channel's state, deterministically: when none is stopped there is nothing to do and
no conversation. Otherwise the agent is asked to start each stopped one; its envelope lets it start an always-on
channel at most once an hour, and stopping, creating or deleting a channel always needs a human.
"""

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from botocore.exceptions import ClientError

from fast_channels.functions import aws

if TYPE_CHECKING:
    from fast_channels.core.ledger import Json

CENSUS_CHANGED: Final = "Census Changed"
ALARM_CHANGED: Final = "CloudWatch Alarm State Change"


def channel_states(channels: Sequence[str]) -> dict[str, str]:
    """Each channel's ``ChannelState``, or the error code that kept it from being read."""
    client = aws.client("mediatailor")
    states: dict[str, str] = {}
    for name in channels:
        try:
            states[name] = str(client.describe_channel(ChannelName=name)["ChannelState"])
        except ClientError as error:
            states[name] = str(error.response.get("Error", {}).get("Code", "ERROR"))
    return states


def trigger_of(event: Mapping[str, Any]) -> tuple[str, dict[str, Json]] | None:
    """The trigger's kind and what it said, for an operations trigger; None for any other event."""
    detail = event.get("detail") or {}
    match event.get("detail-type"):
        case "Census Changed":
            return "census", {"running": detail.get("running"), "previous": detail.get("previous")}
        case "CloudWatch Alarm State Change":
            state = (detail.get("state") or {}).get("value")
            return "alarm", {"alarm": detail.get("alarmName"), "state": state}
    return None


def prompt(kind: str, facts: Mapping[str, Json], stopped: Sequence[str]) -> str:
    """What the operations agent is asked: facts, then the one thing to do."""
    seen = (
        f"The channel census saw the deployment's running channels go from {facts.get('previous')} to "
        f"{facts.get('running')}."
        if kind == "census"
        else f"The probe canary's off-air alarm {facts.get('alarm')} went to {facts.get('state')}."
    )
    names = ", ".join(stopped)
    return (
        f"You are the operations agent of this FAST Channels deployment. {seen} These always-on channels are "
        f"STOPPED: {names}. Always-on channels must be on air (runbook R12). For each of them: read it with "
        "channels___get_channel, start it with channels___start_channel, then read it again and say whether it is "
        "RUNNING. Do not change schedules, other channels or anything else.\n\n"
        f"Facts: {json.dumps({**facts, 'stopped': list(stopped)}, sort_keys=True)}"
    )
