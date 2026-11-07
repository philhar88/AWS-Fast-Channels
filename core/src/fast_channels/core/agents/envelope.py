"""The autonomy envelope v1 (DESIGN-v2.2 §19.3, ADR-0023): what each agent of the roster may do on its own.

The envelope is configuration (``modules.agents.envelope``). For each agent of the roster it lists the tools the agent
may call unattended (each with the channels it may act on, how often, and how far ahead a schedule change must
start), how many approval-gated actions it may take an hour, and the tools that always need a human. Three
enforcement points follow it, so an action outside the envelope is refused even if one of them fails:

- the Gateway's Cedar policies, generated from it per agent principal (``infra.constructs.operator.cedar``): a tool
  call outside the agent's grants is denied;
- the Gateway's request interceptor (``functions.agents.interceptor``), which sees the caller's validated token: an
  agent's call outside :func:`verdict` is refused before it reaches the tool, and an action inside takes one unit of
  each of its caps in one conditional DynamoDB transaction, so no caller, however it reaches the Gateway, exceeds a
  cap or a publish lead;
- the agent runner (``functions.agents.runner``), which answers every approval-gated call of an unattended run with
  the same verdict and a read of the caps, so the usual refusal is recorded before the call is made.

Every action and every refusal is recorded in the ledger, under the envelope's :attr:`Envelope.version`; a refusal is
escalated to a human. v1 is not signed (a KMS signature by the owner is a follow-up, §19.3): its version is the
SHA-256 of its canonical JSON, which each deploy records in the ledger chain ``agents/envelope``.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

TOOL_PATTERN: Final = r"^[a-z]+___[a-z_]+$"
"""A Gateway tool's qualified name, ``<target>___<name>``."""
ALWAYS_ON: Final = "{always_on}"
"""In a grant's ``channels``: the envelope's always-on channels."""
PROGRAMMED: Final = "{programmed}"
"""In a grant's ``channels``: the channels the programming agent keeps scheduled."""
PREFIX: Final = "{prefix}"
WINDOW: Final = timedelta(hours=1)
"""The caps' window: ``per_hour`` and ``actions_per_hour`` count approved actions in each UTC hour."""
LEADS: Final = ("schedule___publish",)
"""Tools whose change starts at a known time (a plan's start), which a grant's ``lead_minutes`` bounds."""
SCOPES: Final[Mapping[str, tuple[str, str]]] = {
    "channels___start_channel": ("channel", "{channel}"),
    "channels___stop_channel": ("channel", "{channel}"),
    "schedule___plan": ("channel", "{channel}"),
    "schedule___publish": ("plan_id", "{channel}.*"),
}
"""Per channel-scoped tool: the input field that names the channel, and its Cedar ``like`` pattern (plan ids are
``<channel>.<hex>``). A grant with ``channels`` must name one of these tools."""


class Role(StrEnum):
    """The roster (DESIGN-v2.2 §19.2)."""

    OPERATIONS = "operations"
    """Watches alarms and runbooks; restarts always-on channels (R12)."""
    PROGRAMMING = "programming"
    """Keeps schedules filled (the v3 autopilot under the envelope)."""
    SALES = "sales"
    """Own-book lines and pacing; propose-only (L1) until 30 days or 20 buys (Y2)."""

    @property
    def principal(self) -> str:
        """The agent's machine principal (ADR-0023), named in every ledger entry it causes."""
        return f"agent-{self.value}"

    @property
    def chain_id(self) -> str:
        """The ledger chain of the agent's actions."""
        return f"agents/{self.value}"


ENVELOPE_CHAIN: Final = "agents/envelope"
"""The ledger chain of the envelope's versions."""


class Level(StrEnum):
    """Autonomy (DESIGN-v2.2 §19.1)."""

    L1 = "L1"
    """Propose only: every approval-gated call is refused and escalated."""
    L2 = "L2"
    """Act inside the envelope."""


class Grant(BaseModel):
    """One tool an agent may call unattended."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str = Field(pattern=TOOL_PATTERN, description="The Gateway tool, ``<target>___<name>``.")
    channels: tuple[str, ...] = Field(
        default=(),
        description="The channels it may act on (``{prefix}`` and ``{always_on}`` are expanded); empty for a tool "
        "that names no channel.",
    )
    per_hour: int | None = Field(
        default=None, ge=0, description="At most this many approved calls per channel in each UTC hour; none: no cap."
    )
    lead_minutes: int | None = Field(
        default=None,
        ge=0,
        description="The change must start at least this many minutes after the call (a published plan's start); "
        "none: any time. Only for the tools of LEADS.",
    )

    @model_validator(mode="after")
    def _scoped(self) -> Self:
        # A channel-scoped grant whose channels are empty (an empty {always_on} once resolved) permits nothing: the
        # verdict refuses it and Cedar has no permit for it, so its per-channel cap is never reached. It must stay
        # valid, because the runner, the interceptor and the deploy's envelope record read the resolved document
        # back.
        if self.channels and self.tool not in SCOPES:
            raise ValueError(f"{self.tool} names no channel: a grant of it cannot list channels")
        if self.lead_minutes is not None and self.tool not in LEADS:
            raise ValueError(f"{self.tool} has no start the envelope can bound: lead_minutes is only for {LEADS}")
        return self


class AgentEnvelope(BaseModel):
    """One agent's part of the envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    level: Level = Level.L2
    grants: tuple[Grant, ...] = ()
    actions_per_hour: int = Field(
        default=10, ge=0, description="Approved approval-gated actions in each UTC hour, all tools."
    )
    spend_usd_per_day: Decimal = Field(
        default=Decimal(0), ge=0, description="US dollars the agent may commit a day (v1 has no tool that spends)."
    )
    needs_human: tuple[str, ...] = Field(
        default=(),
        description="Tools that always need a human: the agent's call is refused and escalated (L3, e.g. R11).",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        tools = [grant.tool for grant in self.grants]
        if duplicate := sorted({tool for tool in tools if tools.count(tool) > 1}):
            raise ValueError(f"one grant per tool: {duplicate}")
        if both := sorted(set(tools) & set(self.needs_human)):
            raise ValueError(f"granted and needing a human at once: {both}")
        return self

    def grant(self, tool: str) -> Grant | None:
        return next((grant for grant in self.grants if grant.tool == tool), None)


def _operations() -> AgentEnvelope:
    return AgentEnvelope(
        enabled=True,
        grants=(Grant(tool="channels___start_channel", channels=(ALWAYS_ON,), per_hour=1),),
        actions_per_hour=6,
        needs_human=(
            "channels___stop_channel",
            "channels___create_channel",
            "channels___delete_channel",
        ),
    )


PUBLISH_LEAD_MINUTES: Final = 120
"""The programming agent publishes only plans that start at least 2 hours ahead; nearer edits are a human's (L3)."""


def _programming() -> AgentEnvelope:
    return AgentEnvelope(
        grants=(
            Grant(tool="schedule___plan", channels=(PROGRAMMED,)),
            Grant(tool="schedule___publish", channels=(PROGRAMMED,), per_hour=6, lead_minutes=PUBLISH_LEAD_MINUTES),
        ),
        actions_per_hour=12,
        needs_human=(
            "schedule___remove_programs",
            "schedule___update_brief",
            "channels___create_channel",
            "channels___delete_channel",
        ),
    )


def _sales() -> AgentEnvelope:
    return AgentEnvelope(level=Level.L1, grants=(Grant(tool="sales___propose", per_hour=12),))


class Envelope(BaseModel):
    """The autonomy envelope v1 (module docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    always_on: tuple[str, ...] = Field(
        default=(),
        description="Channels that should always be on air (``{prefix}`` is expanded): the operations agent restarts "
        "them when they stop (R12). Y2: always-on channels start themselves.",
    )
    programmed: tuple[str, ...] = Field(
        default=(),
        description="Channels the programming agent keeps scheduled (``{prefix}`` is expanded): when one's runway "
        "is under 6 hours it plans and publishes blocks at least 2 hours ahead (R6). A programmed channel has no "
        "autopilot schedule: one writer per channel.",
    )
    agents: dict[Role, AgentEnvelope] = Field(
        default_factory=lambda: {
            Role.OPERATIONS: _operations(),
            Role.PROGRAMMING: _programming(),
            Role.SALES: _sales(),
        }
    )

    def resolved(self, prefix: str) -> Envelope:
        """The envelope as enforced on deployment ``prefix``: ``{prefix}``, ``{always_on}`` and ``{programmed}``
        expanded."""
        always_on = tuple(dict.fromkeys(name.replace(PREFIX, prefix) for name in self.always_on))
        programmed = tuple(dict.fromkeys(name.replace(PREFIX, prefix) for name in self.programmed))
        groups = {ALWAYS_ON: always_on, PROGRAMMED: programmed}

        def channels(names: Sequence[str]) -> tuple[str, ...]:
            expanded = (groups.get(name, (name.replace(PREFIX, prefix),)) for name in names)
            return tuple(dict.fromkeys(name for group in expanded for name in group))

        agents = {
            role: agent.model_copy(
                update={"grants": tuple(g.model_copy(update={"channels": channels(g.channels)}) for g in agent.grants)}
            )
            for role, agent in self.agents.items()
        }
        return Envelope(always_on=always_on, programmed=programmed, agents=agents)

    def enabled(self) -> list[Role]:
        """The roster's agents that are on, in roster order."""
        return [role for role in Role if role in self.agents and self.agents[role].enabled]

    @property
    def document(self) -> str:
        """Its canonical JSON (sorted keys, no spaces): what :attr:`version` hashes and the runner reads."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def version(self) -> str:
        """``sha256:<hex>`` of :attr:`document`."""
        return f"sha256:{hashlib.sha256(self.document.encode()).hexdigest()}"


class Refusal(StrEnum):
    """Why an action is outside the envelope."""

    DISABLED = "agent_disabled"
    PROPOSE_ONLY = "propose_only"
    NEEDS_HUMAN = "needs_human"
    NOT_GRANTED = "not_granted"
    OUT_OF_SCOPE = "out_of_scope"
    LEAD = "lead"
    CAP = "cap"
    UNCHECKED = "unchecked"
    """The envelope could not be checked (a read failed): refused, never allowed unchecked."""


@dataclass(frozen=True, slots=True)
class Cap:
    """One counter an approved action takes a unit of: ``key`` in the window starting at ``window``."""

    key: str
    limit: int
    window: datetime


@dataclass(frozen=True, slots=True)
class Verdict:
    """The envelope's answer about one action: allowed (subject to its caps) or refused, and why."""

    allowed: bool
    reason: str
    refusal: Refusal | None = None
    channel: str | None = None
    caps: tuple[Cap, ...] = ()


def window_of(now: datetime) -> datetime:
    """The start of the caps' window (:data:`WINDOW`, the UTC hour) holding ``now``."""
    return now.replace(minute=0, second=0, microsecond=0)


def like(value: str, pattern: str) -> bool:
    """Cedar's ``like``: ``*`` matches any run of characters, everything else itself."""
    return re.fullmatch(".*".join(re.escape(part) for part in pattern.split("*")), value, re.DOTALL) is not None


def scoped_channel(tool: str, arguments: Mapping[str, Any], channels: Sequence[str]) -> str | None:
    """The one of ``channels`` that ``tool``'s ``arguments`` act on, as its Cedar policy matches it; None if none."""
    field, pattern = SCOPES[tool]
    value = arguments.get(field)
    if not isinstance(value, str):
        return None
    return next((c for c in channels if like(value, pattern.format(channel=c))), None)


def _refusal(agent: AgentEnvelope | None, role: Role, tool: str, *, gated: bool) -> Verdict | None:
    """Why ``role`` may not call ``tool`` at all, whatever its arguments; None when its grant decides."""
    if agent is None or not agent.enabled:
        return Verdict(False, f"the {role.value} agent is off", Refusal.DISABLED)
    if tool in agent.needs_human:
        return Verdict(False, f"{tool} always needs a human", Refusal.NEEDS_HUMAN)
    if gated and agent.level is Level.L1:
        return Verdict(False, f"the {role.value} agent only proposes ({agent.level.value})", Refusal.PROPOSE_ONLY)
    if agent.grant(tool) is None:
        return Verdict(False, f"{tool} is not in the {role.value} agent's envelope", Refusal.NOT_GRANTED)
    return None


def verdict(
    envelope: Envelope,
    role: Role,
    tool: str,
    arguments: Mapping[str, Any],
    *,
    now: datetime,
    gated: bool = True,
    starts: datetime | None = None,
) -> Verdict:
    """Whether ``role`` may call ``tool`` with ``arguments`` unattended, and the caps the action takes.

    ``envelope`` must be :meth:`Envelope.resolved`. ``gated``: the tool is approval-gated (CONFIRM), which a
    propose-only agent may never call and which counts against ``actions_per_hour``. ``starts``: when the change
    starts (a plan's start), for a grant with ``lead_minutes``; unknown, the call is refused. The caps are checked,
    and taken, by the caps' counter (``functions.agents.caps``): this only says which ones apply.
    """
    agent = envelope.agents.get(role)
    grant = agent.grant(tool) if agent is not None else None
    if (refused := _refusal(agent, role, tool, gated=gated)) is not None or agent is None or grant is None:
        return refused or Verdict(False, f"{tool} is not in the {role.value} agent's envelope", Refusal.NOT_GRANTED)
    window = window_of(now)
    caps = [Cap(f"CAP#{role.value}", agent.actions_per_hour, window)] if gated else []
    channel = None
    if tool in SCOPES:  # a channel-scoped tool: only on the grant's channels (none left: on no channel)
        channel = scoped_channel(tool, arguments, grant.channels)
        if channel is None:
            field, _ = SCOPES[tool]
            allowed = ", ".join(grant.channels) or "no channel"
            return Verdict(
                False, f"{tool} on {arguments.get(field)!r}: the envelope allows only {allowed}", Refusal.OUT_OF_SCOPE
            )
        if grant.per_hour is not None:
            caps.append(Cap(f"CAP#{role.value}#{tool}#{channel}", grant.per_hour, window))
    elif grant.per_hour is not None:
        caps.append(Cap(f"CAP#{role.value}#{tool}", grant.per_hour, window))
    if grant.lead_minutes is not None:
        earliest = now + timedelta(minutes=grant.lead_minutes)
        if starts is None or starts < earliest:
            when = "an unknown time" if starts is None else f"{starts:%Y-%m-%dT%H:%M:%SZ}"
            return Verdict(
                False,
                f"{tool}: the change starts at {when}, less than {grant.lead_minutes} minutes ahead; nearer "
                "changes need a human",
                Refusal.LEAD,
                channel,
            )
    return Verdict(True, f"{tool} is inside the {role.value} agent's envelope", channel=channel, caps=tuple(caps))
