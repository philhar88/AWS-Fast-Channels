"""The envelope applied to one tool call of an agent (M9): its verdict, the plan start a publish lead needs, its caps.

Two enforcement points use :func:`check`:

- the Gateway's request interceptor (``functions.agents.interceptor``), on the call itself: an action inside the
  envelope takes one unit of each of its caps (``take=True``), atomically, so no caller of the Gateway with the
  agent's token gets past a cap or a publish lead however it calls the tool;
- the runner (``functions.agents.runner``), before it approves an approval-gated call (``take=False``): it reads the
  caps, so an action that the interceptor would refuse is declined, and recorded, before it is made.

A grant with ``lead_minutes`` bounds when the change starts: for ``schedule___publish`` that is the stored plan's start
(the library table's ``CHANNEL#<channel>`` / ``PLAN#<plan_id>`` item that ``schedule___plan`` wrote), read here
rather than taken from the agent. A plan that is missing or expired has no known start, so its publish is refused.
"""

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fast_channels.core.agents.envelope import LEADS, Envelope, Refusal, Role, Verdict, verdict
from fast_channels.functions import aws
from fast_channels.functions.agents.caps import full, take
from fast_channels.functions.media_library import plan_key


def plan_start(library_table: str, plan_id: str) -> datetime | None:
    """When the stored plan ``plan_id`` (``<channel>.<hex>``) starts; None when it is missing or expired."""
    channel, _, token = plan_id.rpartition(".")
    if not channel or not token:
        return None
    item = aws.table(library_table).get_item(Key=plan_key(channel, plan_id), ConsistentRead=True).get("Item")
    expires, plan = (item or {}).get("expires_at"), (item or {}).get("plan")
    if not isinstance(expires, int | Decimal) or expires < time.time() or not isinstance(plan, dict):
        return None
    start_ms = plan.get("start_ms")
    return datetime.fromtimestamp(int(start_ms) / 1000, UTC) if isinstance(start_ms, int | Decimal) else None


def check(
    envelope: Envelope,
    role: Role,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    now: datetime,
    gated: bool,
    take_caps: bool,
    agents_table: str,
    library_table: str,
) -> Verdict:
    """The verdict on ``role`` calling ``tool`` with ``arguments``; inside the envelope, its caps are then taken
    (``take_caps``) or only read, and a full one refuses the call.

    Raises:
        ClientError: a read or write of the plan or the caps failed (the caller refuses the call).
    """
    agent = envelope.agents.get(role)
    grant = agent.grant(tool) if agent is not None else None
    starts = None
    if tool in LEADS and grant is not None and grant.lead_minutes is not None:
        plan_id = arguments.get("plan_id")
        starts = plan_start(library_table, plan_id) if isinstance(plan_id, str) else None
    found = verdict(envelope, role, tool, arguments, now=now, gated=gated, starts=starts)
    if not found.allowed or not found.caps:
        return found
    client = aws.client("dynamodb")
    hit = take(client, agents_table, found.caps) if take_caps else full(client, agents_table, found.caps)
    if hit is None:
        return found
    return Verdict(False, f"{tool}: the cap of {hit.limit} an hour ({hit.key}) is reached", Refusal.CAP, found.channel)
