"""Cedar policies of the AgentCore Gateway, generated from the agent tool registry (SPEC §6, ADR-0009).

The Gateway authorizes inbound Cognito access tokens (CUSTOM_JWT), so every caller is an ``AgentCore::OAuthUser``
whose JWT claims are principal tags. An array claim such as ``cognito:groups`` becomes its JSON text
(``["fast-operators"]``), so group membership is matched with ``like "*\\"<group>\\"*"`` (AgentCore Policy docs,
"Principal-scoped policies"; verified live with managed-login tokens of each group, a group-less user and the
autopilot's client-credentials token: ``tools/list`` and ``tools/call`` follow the rules below exactly). Permits are
alternatives and forbids always win:

- READ tools: every operator group; WRITE: ``fast-operators`` and ``fast-admins``; DESTRUCTIVE: ``fast-admins``.
- The autopilot client (machine token, no groups) may call READ tools plus :data:`AUTOPILOT_TOOLS`, and is
  forbidden every other approval-gated tool.
- Destructive ``channels`` tools are forbidden for every caller unless the channel is agent-owned
  (``<prefix>-agent-*``), so CloudFormation-owned channels cannot be deleted through the agent.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from aws_cdk import Token

from fast_channels.functions.tools.registry import Access, Approval, ToolSpec


class Group(StrEnum):
    """Cognito groups of operator users, from least to most privileged."""

    VIEWERS = "fast-viewers"
    OPERATORS = "fast-operators"
    ADMINS = "fast-admins"


GROUPS_BY_ACCESS: Final = {
    Access.READ: (Group.VIEWERS, Group.OPERATORS, Group.ADMINS),
    Access.WRITE: (Group.OPERATORS, Group.ADMINS),
    Access.DESTRUCTIVE: (Group.ADMINS,),
}
AUTOPILOT_TOOLS: Final = ("schedule___plan", "schedule___publish")
"""Tools beyond READ the autopilot may call: it extends schedules, nothing else (SPEC §6 Autopilot)."""
AUTOPILOT_CONFIRM_TOOLS: Final = ("schedule___publish",)
"""Approval-gated tools the autopilot may call without a human (its schedule is the approval)."""
CHANNEL_TARGET: Final = "channels"
CHANNEL_FIELD: Final = "channel"
GROUPS_CLAIM: Final = "cognito:groups"
CLIENT_CLAIM: Final = "client_id"
_PRINCIPAL: Final = "principal is AgentCore::OAuthUser"
_SAFE: Final = re.compile(r"[A-Za-z0-9_:/.@*-]+")


@dataclass(frozen=True, slots=True)
class CedarPolicy:
    """One Cedar statement of the Gateway's policy engine."""

    name: str
    """Policy name, unique in the engine (``[A-Za-z][A-Za-z0-9_]*``)."""
    description: str
    statement: str
    forbid: bool
    """Forbids are validated with ``IGNORE_ALL_FINDINGS``: the analyzer flags every intentional deny as
    "Overly Restrictive" (agentcore.md §6.2)."""


def gateway_policies(
    tools: Iterable[ToolSpec[Any, Any]], *, gateway_arn: str, autopilot_client_id: str, agent_channel_prefix: str
) -> list[CedarPolicy]:
    """The Gateway's Cedar policies for ``tools``.

    Args:
        tools: every tool the Gateway exposes.
        gateway_arn: the Gateway ARN; tool-specific policies must name it (``resource ==``).
        autopilot_client_id: Cognito app client id of the autopilot (its tokens' ``client_id`` claim).
        agent_channel_prefix: name prefix of agent-owned channels, e.g. ``fast-agent-``.

    Raises:
        ValueError: an :data:`AUTOPILOT_TOOLS` entry is not a registered tool, or a value would break out of a
            Cedar string literal.
    """
    specs = list(tools)
    names = {spec.qualified_name for spec in specs}
    if missing := sorted(set(AUTOPILOT_TOOLS) - names):
        raise ValueError(f"autopilot tools {missing} are not in the tool registry")
    gateway, autopilot = _literal(gateway_arn), _literal(autopilot_client_id)
    agent_channels = f"{_literal(agent_channel_prefix)}*"

    def named(access: Access) -> list[str]:
        return [spec.qualified_name for spec in specs if spec.access is access]

    policies = [
        CedarPolicy(
            name=f"permit_{access.value}",
            description=f"{access.value.upper()} tools: Cognito groups {', '.join(groups)}",
            statement=_statement("permit", named(access), gateway, when=_in_groups(groups)),
            forbid=False,
        )
        for access, groups in GROUPS_BY_ACCESS.items()
        if named(access)
    ]
    policies.append(
        CedarPolicy(
            name="permit_autopilot",
            description="Autopilot client: READ tools plus schedule planning and publishing",
            statement=_statement("permit", [*named(Access.READ), *AUTOPILOT_TOOLS], gateway, when=_is(autopilot)),
            forbid=False,
        )
    )
    gated = [
        s.qualified_name
        for s in specs
        if s.approval is Approval.CONFIRM and s.qualified_name not in AUTOPILOT_CONFIRM_TOOLS
    ]
    if gated:
        policies.append(
            CedarPolicy(
                name="forbid_autopilot_confirm",
                description="Autopilot client: no approval-gated tool except schedule publishing",
                statement=_statement("forbid", gated, gateway, when=_is(autopilot)),
                forbid=True,
            )
        )
    destructive = [
        s.qualified_name
        for s in specs
        if s.target == CHANNEL_TARGET and s.access is Access.DESTRUCTIVE and CHANNEL_FIELD in s.input.model_fields
    ]
    if destructive:
        policies.append(
            CedarPolicy(
                name="forbid_iac_channels",
                description=f"Destructive channel tools only on agent-owned channels ({agent_channels})",
                statement=_statement(
                    "forbid",
                    destructive,
                    gateway,
                    principal="principal",
                    unless=(
                        f'context.input has {CHANNEL_FIELD} && context.input.{CHANNEL_FIELD} like "{agent_channels}"'
                    ),
                ),
                forbid=True,
            )
        )
    return policies


def _statement(
    effect: str,
    actions: Sequence[str],
    gateway: str,
    *,
    principal: str = _PRINCIPAL,
    when: str | None = None,
    unless: str | None = None,
) -> str:
    action_list = ",\n    ".join(f'AgentCore::Action::"{_literal(a)}"' for a in actions)
    head = (
        f"{effect}(\n  {principal},\n  action in [\n    {action_list}\n  ],\n"
        f'  resource == AgentCore::Gateway::"{gateway}"\n)'
    )
    conditions = [f"{keyword} {{ {body} }}" for keyword, body in (("when", when), ("unless", unless)) if body]
    return "\n".join([head, *conditions]) + ";"


def _in_groups(groups: Sequence[Group]) -> str:
    matches = " || ".join(f'principal.getTag("{GROUPS_CLAIM}") like "*\\"{group}\\"*"' for group in groups)
    return f'principal.hasTag("{GROUPS_CLAIM}") && ({matches})'


def _is(client_id: str) -> str:
    return f'principal.hasTag("{CLIENT_CLAIM}") && principal.getTag("{CLIENT_CLAIM}") == "{client_id}"'


def _literal(value: str) -> str:
    """``value`` for a Cedar string literal: CDK tokens (resolved by CloudFormation) or plain identifiers only."""
    if not Token.is_unresolved(value) and not _SAFE.fullmatch(value):
        raise ValueError(f"{value!r} cannot be embedded in a Cedar policy")
    return value
