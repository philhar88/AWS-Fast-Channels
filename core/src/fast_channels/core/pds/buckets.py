"""Deterministic buckets 0..99 for arms and holdouts.

``bucket(seed, key) = SHA-256(seed ‖ key) mod 100``, the digest read as a big-endian integer. Seeds are versioned and
not secret (``arm-v1``, ``rail-v1``, ``crawl-v1``, ``lineup-<channel>-v1``), so the offline simulator and a holdout
audit recompute every assignment from the decision records alone; changing a seed reshuffles everyone, which is what
a new version of an experiment means.
"""

import hashlib
from typing import Final

ARM_SEED: Final = "arm-v1"
"""Experiment arm per viewer: ``bucket(ARM_SEED, vid)``."""
CRAWL_SEED: Final = "crawl-v1"
"""Content-enrichment holdout per session-break: ``bucket(CRAWL_SEED, "<sid>.<evid>")``."""


def bucket(seed: str, key: str) -> int:
    """``SHA-256(seed ‖ key) mod 100`` (UTF-8, no separator added)."""
    return int.from_bytes(hashlib.sha256(f"{seed}{key}".encode()).digest(), "big") % 100


def in_holdout(seed: str, key: str, percent: int) -> bool:
    """Whether ``key`` falls in the first ``percent`` buckets of ``seed`` (0 holds out nothing, 100 everything)."""
    if not 0 <= percent <= 100:
        raise ValueError(f"percent must be within 0..100, got {percent}")
    return bucket(seed, key) < percent


def crawl_holdout(sid: str, evid: str, percent: int) -> bool:
    """Whether this session-break is in the content-enrichment holdout (no ``app_content`` beyond the constants)."""
    return in_holdout(CRAWL_SEED, f"{sid}.{evid}", percent)
