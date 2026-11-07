"""Names of the creatives module's resources, shared by the stacks (``infra``), the readiness function, the operator
script (``scripts/creatives.py``) and the end-to-end tests, so every side agrees."""

import hashlib
from typing import Final

from fast_channels.core.tags import tag_key

MODULE: Final = "creatives"
MODULE_TAG: Final = tag_key("module")
"""``fast:module=creatives`` marks what the module deploys (inventory)."""
READY_KEY: Final = "creatives/ready.json"
"""The READY creatives in the PDS config bucket (:class:`~fast_channels.core.creatives.registry.ReadyCreatives`),
which the snapshot publisher adds to the own book and the selling module's sync function books."""
INCOMING: Final = "incoming/"
"""The quarantine bucket's prefix of the creatives' source files (one directory per creative)."""
CRID_PREFIX: Final = "cr-"
CRID_PATTERN: Final = r"^cr-[0-9a-f]{12}$"
"""A registered creative's id: never a demo creative's (``sim<n>``, ``direct<n>``) or a house ad's."""
STATE_INDEX: Final = "by-state"
"""The registry's index by state (partition ``state``, sort ``crid``)."""


def table(prefix: str) -> str:
    """The registry table."""
    return f"{prefix}-creatives"


def state_machine(prefix: str) -> str:
    """The readiness state machine: register, moderate, condition, check and publish a creative."""
    return f"{prefix}-creatives"


def quarantine_bucket(prefix: str, account: str, region: str) -> str:
    """The quarantine bucket of the creatives' source files: never the ingest bucket, whose upload rule ingests any
    video under any key, and never a bucket the CDN serves (an unmoderated file is not public)."""
    return f"{prefix}-creatives-{account}-{region}"


def source_key(crid: str, suffix: str = ".mp4") -> str:
    """Quarantine key of a creative's source file."""
    return f"{INCOMING}{crid}/source{suffix}"


def crid_for(adomain: str, source: str) -> str:
    """The id of a creative registered for ``adomain`` from ``source`` (a URL, or a local file's SHA-256): the same
    pair registers the same creative again."""
    return f"{CRID_PREFIX}{hashlib.sha256(f'{adomain}\n{source}'.encode()).hexdigest()[:12]}"
