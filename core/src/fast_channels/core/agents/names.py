"""Names of the agents module's resources, shared by the infrastructure, the functions and the tests."""

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fast_channels.core.agents.envelope import Role

ESCALATIONS_METRIC: Final = "AgentEscalations"
"""Escalations to a human, per agent (namespace ``FastChannels``, dimension ``role``)."""


def table_name(prefix: str) -> str:
    """The agents' table: the envelope caps' counters (``CAP#...`` per window, with a TTL)."""
    return f"{prefix}-agents"


def escalations_alarm(prefix: str, role: Role) -> str:
    """The alarm on ``role``'s escalations (any is a human's to look at)."""
    return f"{prefix}-agent-{role.value}-escalations"
