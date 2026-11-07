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
from fast_channels.core.identity.consent import UNKNOWN, StoredConsent
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


GPP = "DBABMA~CPXxRfAPXxRfAAfKABENB-CgAAAAAAAAAAYgAAAAAAAA"


@pytest.mark.parametrize(
    ("stored", "mode", "reason"),
    [
        (StoredConsent(Consent.FULL, GPP, (2,)), Mode.FULL, Reason.BOUND),
        (StoredConsent(Consent.PSEUDONYMOUS), Mode.PSEUDONYMOUS, Reason.BOUND),
        (StoredConsent(Consent.OPTED_OUT, GPP, (7,)), Mode.CONTEXTUAL, Reason.OPTED_OUT),
        (UNKNOWN, Mode.SAFE, Reason.CONSENT_UNAVAILABLE),
    ],
)
def test_a_signed_in_viewers_consent_decides_their_bound_break_never_the_rows_mode(
    stored: StoredConsent, mode: Mode, reason: Reason
) -> None:
    binding = check(bound(pm="FULL"), sid=SID, pc=PC, now_ms=NOW)
    assert binding.signed_in
    inputs = policy_inputs(binding, stored)
    resolution = resolve(inputs.identity, viewer=inputs.viewer, consent=inputs.consent)
    assert (resolution.mode, resolution.reason) == (mode, reason)
    if mode in {Mode.FULL, Mode.PSEUDONYMOUS}:
        assert resolution.viewer == Viewer(
            vid=VID, cid=row()["cid"], persona="dana", gpp=stored.gpp, gpp_sid=stored.gpp_sid
        )
    elif mode is Mode.CONTEXTUAL:
        assert resolution.viewer == Viewer(cid=row()["cid"], gpp=GPP, gpp_sid=(7,)), "no vid, the GPP string kept"
    else:
        assert resolution.viewer == ANONYMOUS
    assert policy_inputs(binding).consent is Consent.UNAVAILABLE, "a consent nobody read is SAFE"


@pytest.mark.parametrize(
    ("pm", "mode", "reason"),
    [
        ("CONTEXTUAL", Mode.CONTEXTUAL, Reason.BOUND),
        ("OPTED_OUT", Mode.CONTEXTUAL, Reason.OPTED_OUT),
        ("FULL", Mode.SAFE, Reason.CONSENT_UNAVAILABLE),
        (None, Mode.SAFE, Reason.CONSENT_UNAVAILABLE),
    ],
)
def test_a_guests_ticket_carries_its_mode_and_gpp_string(pm: str | None, mode: Mode, reason: Reason) -> None:
    guest = {
        name: value for name, value in bound(pm=pm, gpp=GPP, gpp_sid=[2]).items() if name not in {"vid", "persona"}
    }
    binding = check({name: value for name, value in guest.items() if value is not None}, sid=SID, pc=PC, now_ms=NOW)
    assert binding.bound and not binding.signed_in
    inputs = policy_inputs(binding, StoredConsent(Consent.FULL))  # a guest has no consent row: it is never used
    resolution = resolve(inputs.identity, viewer=inputs.viewer, consent=inputs.consent)
    assert (resolution.mode, resolution.reason) == (mode, reason)
    if mode is Mode.CONTEXTUAL:
        assert resolution.viewer == Viewer(cid=row()["cid"], gpp=GPP, gpp_sid=(2,))


def test_a_failed_lookup_is_safe_never_contextual() -> None:
    resolution = resolve(LOOKUP_FAILED.identity, viewer=LOOKUP_FAILED.viewer, consent=LOOKUP_FAILED.consent)
    assert (resolution.mode, resolution.reason, resolution.viewer) == (Mode.SAFE, Reason.CONSENT_UNAVAILABLE, ANONYMOUS)
    assert LOOKUP_FAILED.consent is Consent.UNAVAILABLE
