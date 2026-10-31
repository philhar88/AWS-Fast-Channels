"""What each runbook would do about its trigger, and its kill switch."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from fast_channels.core.flags import OpsSwitches
from fast_channels.core.ledger import CHAIN_ID_PATTERN
from fast_channels.core.runbooks import (
    KILL_SWITCHES,
    MAX_INGEST_ATTEMPTS,
    Action,
    Mode,
    Runbook,
    ca_alert_actions,
    cdn_actions,
    decide,
    ingest_failure_actions,
    missing_source_actions,
    off_air_actions,
    qoe_actions,
    quota_actions,
    rights_actions,
)

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("runbook", list(Runbook))
def test_every_runbook_has_a_valid_chain(runbook: Runbook) -> None:
    assert re.fullmatch(CHAIN_ID_PATTERN, runbook.chain_id)
    assert runbook.chain_id == f"runbook/{runbook.value}"


def test_the_kill_switches_are_the_designs() -> None:
    assert {r.value: s for r, s in KILL_SWITCHES.items()} == {
        "r9": "freeze",
        "r12": "restart",
        "r13": "ca_alerts",
        "r17": "shed",
    }


@pytest.mark.parametrize("runbook", list(KILL_SWITCHES))
def test_a_runbook_whose_switch_is_off_is_disabled(runbook: Runbook) -> None:
    switches = OpsSwitches(**{KILL_SWITCHES[runbook]: False})

    decision = decide(runbook, switches, [Action.PAGE], subject="fastm-demo", reason="it broke")

    assert (decision.mode, decision.actions) == (Mode.DISABLED, ())
    assert decision.reason == f"kill switch ops.{KILL_SWITCHES[runbook]} is off; would have: it broke"


def test_every_runbook_runs_its_dry_run_path_by_default() -> None:
    decision = decide(Runbook.R7, OpsSwitches(), [Action.DRAFT_QUOTA_CASE], subject="a", reason="b", facts={"x": 1})

    assert decision.mode is Mode.DRY_RUN
    assert decision.detail() == {
        "runbook": "r7",
        "mode": "dry_run",
        "actions": ["quota.draft_case"],
        "reason": "b",
        "facts": {"x": 1},
    }


def test_alarm_runbooks_act_only_on_alarm() -> None:
    for actions in (quota_actions, qoe_actions, cdn_actions):
        assert actions("OK")[0] == [Action.NONE]
    assert quota_actions("ALARM")[0] == [Action.FREEZE_EXPANSION, Action.DRAFT_QUOTA_CASE]
    assert qoe_actions("ALARM")[0] == [Action.FREEZE_EXPERIMENTS, Action.FREEZE_RELEASES]
    assert cdn_actions("ALARM")[0] == [Action.PAGE, Action.SHED]


def test_r12_starts_only_a_stopped_always_on_channel() -> None:
    channels = {"fastm-demo": "STOPPED", "fastm-news": "RUNNING"}

    assert off_air_actions(channels, ["fastm-demo"])[0] == [Action.START_CHANNEL]
    assert off_air_actions(channels, ["fastm-news"])[0] == [Action.PAGE_SEV1]
    actions, reason = off_air_actions(channels, [])
    assert actions == [Action.PAGE_SEV1]
    assert "no always-on list yet" in reason


def test_r13_swaps_scheduling_errors_and_pages_on_repeat() -> None:
    assert ca_alert_actions("INFO", "NOT_PROCESSED", repeated=False, evergreen="evergreen")[0] == [Action.NONE]
    assert ca_alert_actions("SCHEDULING_ERROR", "X", repeated=False, evergreen="evergreen")[0] == [
        Action.SWAP_EVERGREEN
    ]
    assert ca_alert_actions("SCHEDULING_ERROR", "X", repeated=True, evergreen="evergreen")[0] == [Action.PAGE]
    actions, reason = ca_alert_actions("SCHEDULING_ERROR", "X", repeated=False, evergreen=None)
    assert (actions, reason) == ([Action.PAGE], "X: no evergreen block configured")


def test_r14_retries_once_then_quarantines() -> None:
    assert ingest_failure_actions(1)[0] == [Action.RETRY_INGEST]
    assert ingest_failure_actions(MAX_INGEST_ATTEMPTS)[0] == [Action.QUARANTINE_ASSET, Action.NOTIFY_CMS]


def test_r15_replaces_by_agent_ahead_and_by_evergreen_close_in() -> None:
    assert missing_source_actions(NOW + timedelta(hours=3), NOW, evergreen=None)[0] == [Action.REPLACE_BY_AGENT]
    assert missing_source_actions(NOW + timedelta(hours=1), NOW, evergreen="e")[0] == [Action.SWAP_EVERGREEN]
    assert missing_source_actions(NOW + timedelta(hours=1), NOW, evergreen=None)[0] == [Action.PAGE]


def test_r16_replaces_a_program_that_ends_after_its_rights() -> None:
    ends = NOW + timedelta(hours=4)

    assert rights_actions(ends, None)[0] == [Action.NONE]
    assert rights_actions(ends, ends)[0] == [Action.NONE]
    actions, reason = rights_actions(ends, ends - timedelta(minutes=1))
    assert actions == [Action.REPLACE_BY_AGENT]
    assert "before the program ends" in reason
