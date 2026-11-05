"""The pure readers of ``harness_identity``."""

import base64
import json

import pytest
from harness_identity import claims, ticket_of

TICKET = "AbCdEfGhIjKlMnOpQr_-12"


def test_ticket_of_takes_the_one_valid_ticket() -> None:
    assert ticket_of(f"https://cdn.example/v1/master/h/pc/hls.m3u8?ads.t={TICKET}") == TICKET
    for url in (
        "https://cdn.example/v1/master/h/pc/hls.m3u8",
        f"https://cdn.example/hls.m3u8?ads.t={TICKET}&ads.t={TICKET}",
        f"https://cdn.example/hls.m3u8?ads.t={TICKET[:21]}",
    ):
        with pytest.raises(ValueError, match=r"ads\.t"):
            ticket_of(url)


def test_claims_reads_an_unpadded_payload() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"token_use": "access", "scope": "fast/playback"}).encode())
    token = f"h.{payload.decode().rstrip('=')}.s"
    assert claims(token) == {"token_use": "access", "scope": "fast/playback"}
