"""The identity module's core: the playback ticket, its row and the viewer id."""

import hashlib
import hmac

import pytest

from fast_channels.core.identity import names
from fast_channels.core.identity.ticket import (
    REDEEM_CONDITION,
    ROW_TTL_S,
    TICKET,
    PrivacyMode,
    key,
    mint,
    ticket_row,
)
from fast_channels.core.identity.vid import VID_PATTERN, message, vid_from_mac, viewer_id

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
SUB = "0f6b4c7e-1a2b-4c3d-9e8f-001122334455"


def test_a_ticket_is_22_url_safe_characters_without_doubles() -> None:
    tickets = {mint() for _ in range(20_000)}
    assert len(tickets) == 20_000, "128 random bits do not collide"
    assert all(TICKET.fullmatch(t) and "--" not in t and "__" not in t for t in tickets)


def test_a_double_is_redrawn(monkeypatch: pytest.MonkeyPatch) -> None:
    draws = iter(["A" * 10 + "--" + "B" * 10, "C" * 10 + "__" + "D" * 10, "E" * 22])
    monkeypatch.setattr("fast_channels.core.identity.ticket.secrets.token_urlsafe", lambda n: next(draws))
    assert mint() == "E" * 22


def test_the_row_of_a_ticket() -> None:
    row = ticket_row(ticket="A" * 22, pc="fastm-demo-p-a", cid="c-1", now_ms=1_000_000, vid="ab" * 16, persona="dana")
    assert row.item() == {
        "pk": "T#" + "A" * 22,
        "pc": "fastm-demo-p-a",
        "cid": "c-1",
        "iat": 1_000_000,
        "redeem_by": 1_120_000,
        "ttl": 1_000 + ROW_TTL_S,
        "vid": "ab" * 16,
        "persona": "dana",
    }
    assert "pm" not in row.item(), "a signed-in viewer's mode is their consent, read on every break"
    guest = ticket_row(
        ticket="B" * 22, pc="fastm-demo-p-a", cid="c-2", now_ms=0, pm=PrivacyMode.OPTED_OUT, gpp="DBAA", gpp_sid=(7,)
    )
    assert "vid" not in guest.item() and "persona" not in guest.item()
    assert (guest.item()["pm"], guest.item()["gpp"], guest.item()["gpp_sid"]) == ("OPTED_OUT", "DBAA", [7])
    assert "gpp_sid" not in ticket_row(ticket="C" * 22, pc="fastm-demo-p-a", cid="c-3", now_ms=0).item()
    with pytest.raises(ValueError, match="not a playback ticket"):
        key("short")


def test_the_redeem_binds_once_per_session_inside_the_window() -> None:
    """The condition the redeem and (in B1-2) the PDS self-redeem share (DESIGN-v2.2 §5.3)."""
    assert REDEEM_CONDITION == (
        "attribute_exists(pk) AND pc = :pc AND ((attribute_not_exists(redeemedBy) AND redeem_by > :now) "
        "OR redeemedBy = :sid)"
    )


def test_the_vid_is_a_truncated_hmac_of_issuer_and_subject() -> None:
    assert message(ISSUER, SUB) == f"{ISSUER}|{SUB}".encode()
    secret = b"k" * 32
    mac = hmac.new(secret, message(ISSUER, SUB), hashlib.sha256).digest()
    vid = vid_from_mac(mac)
    assert VID_PATTERN.fullmatch(vid) and vid == mac[:16].hex()
    for issuer, sub in (("", SUB), (ISSUER, ""), (ISSUER, "a|b"), ("x|y", SUB)):
        with pytest.raises(ValueError, match="free of"):
            message(issuer, sub)
    with pytest.raises(ValueError, match="shorter than a vid"):
        vid_from_mac(b"x" * 8)

    class Kms:
        def generate_mac(self, *, KeyId: str, MacAlgorithm: str, Message: bytes) -> dict[str, bytes]:  # noqa: N803
            assert (KeyId, MacAlgorithm) == ("arn:key", "HMAC_SHA_256")
            return {"Mac": hmac.new(secret, Message, hashlib.sha256).digest()}

    assert viewer_id(Kms(), key_id="arn:key", issuer=ISSUER, sub=SUB) == vid


def test_the_module_names() -> None:
    assert names.PLAYBACK_SCOPE == "fast/playback"
    assert names.parameter_name("fastm", names.Parameter.REDEEM_URL) == "/fastm/identity/redeem-url"
    assert names.table_name("fastm") == "fastm-identity"
    assert names.vid_key_alias("fastm") == "alias/fastm-identity-vid"
    assert names.demo_users_secret("fastm") == "fastm/identity/demo-users"
