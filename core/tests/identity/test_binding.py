"""The binding check (``core.identity.binding``): one ticket row, one break of one session."""

from typing import Any

import pytest

from fast_channels.core.identity.binding import (
    ABSOLUTE_MS,
    IDLE_MS,
    LOOKUP_FAILED,
    REFRESH_MS,
    Action,
    Binding,
    PolicyInputs,
    check,
    policy_inputs,
    refresh,
    self_redeem,
)
from fast_channels.core.identity.ticket import REDEEM_CONDITION, REDEEM_UPDATE, PrivacyMode
from fast_channels.core.pds.models import Mode, Reason
from fast_channels.core.pds.policy import ANONYMOUS, Consent, Viewer, resolve

SID, PC, NOW = "6f1c2a9e-4b7d-4e2a-9c1e-2d3f4a5b6c7d", "fastm-demo-p-a", 1_800_000_000_000
TICKET = "AbCdEfGhIjKlMnOpQr_-12"
VID = "0" * 31 + "a"


def row(**fields: Any) -> dict[str, Any]:
    """A minted row, 30 s ago, redeemable for 90 s more."""
    return {
        "pk": f"T#{TICKET}",
        "pc": PC,
        "pm": "PSEUDONYMOUS",
        "cid": "01938f6e-0000-7000-8000-000000000001",
        "iat": NOW - 30_000,
        "redeem_by": NOW + 90_000,
        "vid": VID,
        "persona": "dana",
        **fields,
    }


def bound(**fields: Any) -> dict[str, Any]:
    """The row bound to ``SID`` a minute ago."""
    return row(**{"redeemedBy": SID, "bound_at": NOW - 60_000, "last_seen": NOW - 60_000, **fields})


def test_a_row_bound_to_this_session_and_configuration_is_bound() -> None:
    binding = check(bound(), sid=SID, pc=PC, now_ms=NOW)
    assert binding == Binding(
        Reason.BOUND, vid=VID, cid=row()["cid"], persona="dana", pm=PrivacyMode.PSEUDONYMOUS, action=Action.NONE
    )


@pytest.mark.parametrize(
    ("found", "reason"),
    [
        (None, Reason.UNKNOWN),
        (row(pc="fastm-demo-p-b"), Reason.PC_MISMATCH),
        (bound(redeemedBy="another-session"), Reason.REPLAY),
        (row(redeem_by=NOW), Reason.EXPIRED_OR_RACED),  # the window closed unredeemed
        (bound(last_seen=NOW - IDLE_MS), Reason.EXPIRED_OR_RACED),  # idle 2 h
        (bound(bound_at=NOW - ABSOLUTE_MS, last_seen=NOW - 1), Reason.EXPIRED_OR_RACED),  # 24 h
    ],
    ids=["no row", "another configuration", "replay", "never redeemed", "idle lease", "absolute lease"],
)
def test_anything_else_is_anonymous_with_the_reason(found: dict[str, Any] | None, reason: Reason) -> None:
    binding = check(found, sid=SID, pc=PC, now_ms=NOW)
    assert (binding.reason, binding.vid, binding.action) == (reason, None, Action.NONE)
    assert policy_inputs(binding) == PolicyInputs(reason)


def test_a_fresh_unredeemed_row_is_self_redeemed() -> None:
    binding = check(row(), sid=SID, pc=PC, now_ms=NOW)
    assert (binding.reason, binding.vid, binding.action) == (Reason.BOUND, VID, Action.SELF_REDEEM)


def test_last_seen_is_renewed_every_ten_minutes_at_most() -> None:
    assert check(bound(last_seen=NOW - REFRESH_MS + 1), sid=SID, pc=PC, now_ms=NOW).action is Action.NONE
    assert check(bound(last_seen=NOW - REFRESH_MS), sid=SID, pc=PC, now_ms=NOW).action is Action.REFRESH


def test_the_writes_are_the_identity_apis_own() -> None:
    redeem = self_redeem("fastm-identity", TICKET, sid=SID, pc=PC, now_ms=NOW)
    assert (redeem["ConditionExpression"], redeem["UpdateExpression"]) == (REDEEM_CONDITION, REDEEM_UPDATE)
    assert redeem["Key"] == {"pk": {"S": f"T#{TICKET}"}}
    assert redeem["ExpressionAttributeValues"] == {":sid": {"S": SID}, ":pc": {"S": PC}, ":now": {"N": str(NOW)}}
    renew = refresh("fastm-identity", TICKET, sid=SID, now_ms=NOW)
    assert (renew["ConditionExpression"], renew["UpdateExpression"]) == ("redeemedBy = :sid", "SET last_seen = :now")


@pytest.mark.parametrize(
    ("pm", "mode"),
    [("PSEUDONYMOUS", Mode.PSEUDONYMOUS), ("FULL", Mode.FULL), ("OPTED_OUT", Mode.CONTEXTUAL), ("WHAT", Mode.SAFE)],
)
def test_the_rows_privacy_mode_decides_a_bound_break(pm: str, mode: Mode) -> None:
    inputs = policy_inputs(check(bound(pm=pm), sid=SID, pc=PC, now_ms=NOW))
    resolution = resolve(inputs.identity, viewer=inputs.viewer, consent=inputs.consent)
    assert resolution.mode is mode
    if mode in {Mode.FULL, Mode.PSEUDONYMOUS}:
        assert resolution.viewer == Viewer(vid=VID, cid=row()["cid"], persona="dana")
    else:
        assert resolution.viewer.vid is None


def test_a_failed_lookup_is_safe_never_contextual() -> None:
    resolution = resolve(LOOKUP_FAILED.identity, viewer=LOOKUP_FAILED.viewer, consent=LOOKUP_FAILED.consent)
    assert (resolution.mode, resolution.reason, resolution.viewer) == (Mode.SAFE, Reason.CONSENT_UNAVAILABLE, ANONYMOUS)
    assert LOOKUP_FAILED.consent is Consent.UNAVAILABLE
