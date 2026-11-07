"""The sales agent's trigger (M9): an own-book line that is overpacing or underpacing.

An hourly rule starts the sales agent's runner. The runner first reads, deterministically, every line's pacing
(``functions.tools.sales.pacings``: the published PDS snapshot and the lines' budget shards): when every line is on
pace, or cannot be judged, there is nothing to do and no conversation. Otherwise the agent is asked to look at the
off-pace lines and propose a change for each with ``sales___propose``. The agent is propose-only (L1): it has no tool
that commits a change, and every proposal it records is escalated to a human, who decides.
"""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Final

from fast_channels.core.pds.pacing import OVER, UNDER
from fast_channels.functions.tools.sales import line_pace, pacings

if TYPE_CHECKING:
    from fast_channels.core.ledger import Json
    from fast_channels.core.pds.pacing import Pacing

PROPOSE: Final = "sales___propose"


def facts(at: datetime) -> tuple[dict[str, Json], list[Pacing]]:
    """What the trigger knew (the snapshot and every line's pacing), and the off-pace lines."""
    version, found = pacings(at)
    paces: dict[str, Json] = {p.line: json.loads(line_pace(p).model_dump_json()) for p in found}
    return {"snapshot": version, "lines": paces}, [p for p in found if p.state.off_pace]


def prompt(known: dict[str, Json], off: Sequence[Pacing]) -> str:
    """What the sales agent is asked: facts, then the one thing to do."""
    lines = "\n".join(
        f"- {p.line} ({p.advertiser}): {p.state.value}, pace {p.pace:.2f}, {p.spent} of {p.budget} US dollars spent "
        f"with {round((p.elapsed or 0) * 100)} percent of its flight gone"
        for p in off
    )
    return (
        "You are the sales agent of this FAST Channels deployment's own book. These lines are off pace (pace above "
        f"{OVER} or below {UNDER}):\n{lines}\n\n"
        "For each line: read its pacing with sales___line_pacing, then record one proposal with sales___propose "
        "that would bring it back on pace (pause, end or extend its flight, change its budget, or cap it per "
        "session), with the reason. You only propose: a human decides and makes the change.\n\n"
        f"Facts: {json.dumps(known, sort_keys=True)}"
    )
