"""The consent store's rows and how the policy reads them; the guests' rule (``core.identity.consent``)."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fast_channels.core.identity.consent import (
    MAX_AGE,
    UNKNOWN,
    ConsentChoice,
    StoredConsent,
    consent_of,
    guest_mode,
    key,
    record,
    stored,
)
from fast_channels.core.identity.ticket import PrivacyMode
from fast_channels.core.pds.policy import Consent

VID = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
GPP = "DBABMA~CPXxRfAPXxRfAAfKABENB-CgAAAAAAAAAAYgAAAAAAAA"


@pytest.mark.parametrize(
    ("choice", "consent"),
    [
        (ConsentChoice(opt_out=True, vppa=True), Consent.OPTED_OUT),
        (ConsentChoice(opt_out=False, vppa=True), Consent.FULL),
        (ConsentChoice(opt_out=False), Consent.PSEUDONYMOUS),
    ],
    ids=["an opt-out wins", "video-privacy consent", "neither"],
)
def test_a_choice_maps_to_the_consent_the_policy_reads(choice: ConsentChoice, consent: Consent) -> None:
    assert consent_of(record(VID, choice, now_ms=NOW_MS), now=NOW) is consent


def test_a_missing_or_stale_record_is_unavailable() -> None:
    assert consent_of(None, now=NOW) is Consent.UNAVAILABLE
    row = record(VID, ConsentChoice(opt_out=False, vppa=True), now_ms=NOW_MS)
    assert consent_of(row, now=NOW + MAX_AGE) is Consent.FULL
    assert consent_of(row, now=NOW + MAX_AGE + timedelta(milliseconds=1)) is Consent.UNAVAILABLE
    assert stored(row, now=NOW + MAX_AGE + timedelta(seconds=1)) == UNKNOWN


def test_the_row_and_the_gpp_string_as_stored() -> None:
    row = record(VID, ConsentChoice(opt_out=True, gpp=GPP, gpp_sid=(7, 8)), now_ms=NOW_MS)
    assert row.item() == {
        "pk": key(VID),
        "opt_out": True,
        "vppa": False,
        "gpp": GPP,
        "gpp_sid": [7, 8],
        "updated_at": NOW_MS,
    }
    assert stored(row, now=NOW) == StoredConsent(Consent.OPTED_OUT, GPP, (7, 8))
    assert "gpp" not in record(VID, ConsentChoice(opt_out=False), now_ms=NOW_MS).item()


@pytest.mark.parametrize("gpp", ["", "a b", "x" * 4097, 'DBA"}'])
def test_a_gpp_string_is_held_to_its_alphabet(gpp: str) -> None:
    with pytest.raises(ValidationError):
        ConsentChoice(opt_out=False, gpp=gpp)


def test_a_guest_is_contextual_or_opted_out_with_gpc() -> None:
    assert guest_mode(gpc=False) is PrivacyMode.CONTEXTUAL
    assert guest_mode(gpc=True) is PrivacyMode.OPTED_OUT
