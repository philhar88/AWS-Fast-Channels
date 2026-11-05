"""The viewer id ``vid``: a keyed hash of the token's issuer and subject (DESIGN-v2.2 §5.1).

``vid = HMAC-SHA256(k, iss | sub)`` with a KMS HMAC key, truncated to 128 bits and written as 32 lowercase hex
characters. The key never leaves KMS, so the ``vid`` of a ``sub`` can only be computed with ``kms:GenerateMac`` on
that key. Only the ``vid`` reaches the ticket row, the Pod Decision Service, its records and the lake; the raw
``sub`` never does. The mint and the demo-user seeding compute it with :func:`viewer_id`, the e2e tests too.
"""

import re
from collections.abc import Mapping
from typing import Any, Final, Protocol

MAC_ALGORITHM: Final = "HMAC_SHA_256"
VID_PATTERN: Final = re.compile(r"[0-9a-f]{32}")
"""A ``vid``: 128 bits as lowercase hex."""
SEPARATOR: Final = "|"
"""Between the issuer and the subject. Neither a Cognito issuer URL nor a ``sub`` (a UUID) contains it."""


def message(issuer: str, sub: str) -> bytes:
    """The MAC input of a viewer: ``<issuer>|<sub>``.

    Raises:
        ValueError: ``issuer`` or ``sub`` is empty or contains the separator.
    """
    if not issuer or not sub or SEPARATOR in issuer or SEPARATOR in sub:
        raise ValueError("issuer and sub must be non-empty and free of '|'")
    return f"{issuer}{SEPARATOR}{sub}".encode()


def vid_from_mac(mac: bytes) -> str:
    """The ``vid`` of a MAC: its first 16 bytes, as hex.

    Raises:
        ValueError: ``mac`` is shorter than 16 bytes.
    """
    if len(mac) < 16:
        raise ValueError(f"a MAC of {len(mac)} bytes is shorter than a vid")
    return mac[:16].hex()


class MacClient(Protocol):
    """What :func:`viewer_id` needs of a boto3 KMS client."""

    def generate_mac(self, *, KeyId: str, MacAlgorithm: str, Message: bytes) -> Mapping[str, Any]: ...  # noqa: N803


def viewer_id(kms: MacClient, *, key_id: str, issuer: str, sub: str) -> str:
    """The ``vid`` of the viewer ``sub`` of ``issuer``: ``kms:GenerateMac`` on :func:`message`, truncated."""
    response = kms.generate_mac(KeyId=key_id, MacAlgorithm=MAC_ALGORITHM, Message=message(issuer, sub))
    mac: bytes = response["Mac"]
    return vid_from_mac(mac)
