"""The ops module's runbooks R7, R9 and R12-R17 (D§18.8): what each one does about its trigger, decided from facts.

Each runbook is an allow-listed Step Functions state machine that a trigger starts (an alarm, a ``Channel Alert``,
an ``Asset Failed`` or ``Schedule Published`` event, a schedule). Its function gathers the facts and calls the
decision here, and every execution appends one ledger record (chain ``runbook/<id>``) of what it decided. Acting
is L2 autonomy, which needs ADR-0023 accepted, so until then every runbook runs its dry-run path: it records the
actions it would take (``mode`` ``dry_run``). A runbook whose kill switch is off records ``disabled``.

| Id | Trigger | Actions it would take | Kill switch |
|---|---|---|---|
| R7 | An account quota alarm (80%) | Freeze expansion; draft a quota case that a human submits | — |
| R9 | A QoE proxy alarm (``GetManifest`` errors) | Freeze experiments and releases | ``ops.freeze`` |
| R12 | The probe failed twice in a row | Start a stopped always-on channel; otherwise page SEV1 | ``ops.restart`` |
| R13 | A Channel Assembly alert | Scheduling error: swap in the evergreen block; page on repeat | ``ops.ca_alerts`` |
| R14 | An ingest failure (``Asset Failed``) | Retry once; then quarantine and notify the CMS | — |
| R15 | A published program's VOD source is missing | ≥2 h ahead: the agent replaces it; else evergreen | — |
| R16 | A program within 6 h ends after its rights | The programming agent replaces it | — |
| R17 | The CDN 5xx alarm | Page; shed optional work | ``ops.shed`` |

There is no evergreen block and no always-on list yet (the envelope, OD10), so R12, R13 and R15 page instead.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from fast_channels.core.ledger import Json  # noqa: TC001 - pydantic resolves field types at runtime

if TYPE_CHECKING:
    from fast_channels.core.flags import OpsSwitches


class Runbook(StrEnum):
    R7 = "r7"
    R9 = "r9"
    R12 = "r12"
    R13 = "r13"
    R14 = "r14"
    R15 = "r15"
    R16 = "r16"
    R17 = "r17"

    @property
    def chain_id(self) -> str:
        """The runbook's ledger chain."""
        return f"runbook/{self.value}"


KILL_SWITCHES: Final[dict[Runbook, str]] = {
    Runbook.R9: "freeze",
    Runbook.R12: "restart",
    Runbook.R13: "ca_alerts",
    Runbook.R17: "shed",
}
"""The ``ops`` flag attribute that disables each runbook (D§18.8); the others stop with their trigger."""
REPLACE_LEAD: Final = timedelta(hours=2)
"""R15: the programming agent replaces a program at least this far ahead; nearer, the evergreen block does."""
RIGHTS_LOOKAHEAD: Final = timedelta(hours=6)
"""R16 checks the programs that start within this window."""
REPEAT_WINDOW: Final = timedelta(hours=1)
"""R13 pages when the same alert came back within this window."""
MAX_INGEST_ATTEMPTS: Final = 2
"""R14: the failed attempt plus one retry."""


class Mode(StrEnum):
    DRY_RUN = "dry_run"
    DISABLED = "disabled"


class Action(StrEnum):
    """What a runbook would do."""

    NONE = "none"
    FREEZE_EXPANSION = "ops.freeze_expansion"
    DRAFT_QUOTA_CASE = "quota.draft_case"
    FREEZE_RELEASES = "release.freeze"
    FREEZE_EXPERIMENTS = "experiments.freeze"
    START_CHANNEL = "channel.start"
    PAGE = "page"
    PAGE_SEV1 = "page.sev1"
    SWAP_EVERGREEN = "program.swap_evergreen"
    REPLACE_BY_AGENT = "program.replace_by_agent"
    RETRY_INGEST = "ingest.retry"
    QUARANTINE_ASSET = "asset.quarantine"
    NOTIFY_CMS = "cms.notify"
    SHED = "ops.shed"


class Decision(BaseModel):
    """What one execution of a runbook decided: its ledger record's content."""

    model_config = ConfigDict(frozen=True)

    runbook: Runbook
    mode: Mode
    actions: tuple[Action, ...]
    subject: str = Field(min_length=1, max_length=512)
    reason: str
    facts: dict[str, Json] = Field(default_factory=dict)

    def detail(self) -> dict[str, Json]:
        """The ledger record's ``detail``."""
        return {
            "runbook": self.runbook.value,
            "mode": self.mode.value,
            "actions": [action.value for action in self.actions],
            "reason": self.reason,
            "facts": self.facts,
        }


def decide(
    runbook: Runbook,
    switches: OpsSwitches,
    actions: Sequence[Action],
    *,
    subject: str,
    reason: str,
    facts: Mapping[str, Json] | None = None,
) -> Decision:
    """The decision of ``runbook``: its dry run of ``actions``, or ``disabled`` when its kill switch is off."""
    switch = KILL_SWITCHES.get(runbook)
    if switch is not None and not getattr(switches, switch):
        return Decision(
            runbook=runbook,
            mode=Mode.DISABLED,
            actions=(),
            subject=subject,
            reason=f"kill switch ops.{switch} is off; would have: {reason}",
            facts=dict(facts or {}),
        )
    return Decision(
        runbook=runbook,
        mode=Mode.DRY_RUN,
        actions=tuple(actions),
        subject=subject,
        reason=reason,
        facts=dict(facts or {}),
    )


# --- the actions of each runbook (pure) ----------------------------------------------------------------------------


def quota_actions(state: str) -> tuple[list[Action], str]:
    """R7, for an account quota alarm in ``state``."""
    if state != "ALARM":
        return [Action.NONE], f"the quota alarm is {state}"
    return [Action.FREEZE_EXPANSION, Action.DRAFT_QUOTA_CASE], "account requests above 80% of the quota"


def qoe_actions(state: str) -> tuple[list[Action], str]:
    """R9, for a QoE proxy alarm in ``state``."""
    if state != "ALARM":
        return [Action.NONE], f"the QoE alarm is {state}"
    return [Action.FREEZE_EXPERIMENTS, Action.FREEZE_RELEASES], "a QoE proxy breached"


def off_air_actions(channels: Mapping[str, str], always_on: Sequence[str]) -> tuple[list[Action], str]:
    """R12: ``channels`` maps each IaC channel to its state when the probe failed twice."""
    stopped = sorted(name for name, state in channels.items() if state == "STOPPED" and name in always_on)
    if stopped:
        return [Action.START_CHANNEL], f"always-on channel(s) {', '.join(stopped)} found stopped"
    listed = "no always-on list yet" if not always_on else "no listed channel is stopped"
    return [Action.PAGE_SEV1], f"probe stalled on 2 consecutive polls ({listed})"


def ca_alert_actions(category: str, code: str, *, repeated: bool, evergreen: str | None) -> tuple[list[Action], str]:
    """R13, for a Channel Assembly alert; ``repeated`` when the same alert came back within the hour."""
    if category != "SCHEDULING_ERROR":
        return [Action.NONE], f"{category} {code}: no action"
    if repeated:
        return [Action.PAGE], f"{code} came back within {REPEAT_WINDOW}"
    if evergreen is None:
        return [Action.PAGE], f"{code}: no evergreen block configured"
    return [Action.SWAP_EVERGREEN], f"{code}: swap the program for {evergreen}"


def ingest_failure_actions(attempts: int) -> tuple[list[Action], str]:
    """R14, after ``attempts`` failed ingests of the asset (this one included)."""
    if attempts < MAX_INGEST_ATTEMPTS:
        return [Action.RETRY_INGEST], f"failed attempt {attempts}: retry once with the same settings"
    return [Action.QUARANTINE_ASSET, Action.NOTIFY_CMS], f"failed {attempts} times: quarantine and notify the CMS"


def missing_source_actions(starts_at: datetime, now: datetime, *, evergreen: str | None) -> tuple[list[Action], str]:
    """R15, for a published program whose VOD source is missing."""
    lead = starts_at - now
    if lead >= REPLACE_LEAD:
        return [Action.REPLACE_BY_AGENT], f"starts in {lead}: the programming agent replaces it"
    if evergreen is None:
        return [Action.PAGE], f"starts in {lead}: no evergreen block configured"
    return [Action.SWAP_EVERGREEN], f"starts in {lead}: swap it for {evergreen}"


def rights_actions(ends_at: datetime, available_until: datetime | None) -> tuple[list[Action], str]:
    """R16, for a program starting within :data:`RIGHTS_LOOKAHEAD`."""
    if available_until is None or available_until >= ends_at:
        return [Action.NONE], "inside its rights window"
    return [Action.REPLACE_BY_AGENT], f"rights end at {available_until.isoformat()}, before the program ends"


def cdn_actions(state: str) -> tuple[list[Action], str]:
    """R17, for the CDN 5xx alarm in ``state``."""
    if state != "ALARM":
        return [Action.NONE], f"the CDN alarm is {state}"
    return [Action.PAGE, Action.SHED], "CDN 5xx rate above its threshold"
