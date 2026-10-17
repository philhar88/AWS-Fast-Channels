"""MediaConvert encode cost estimates (us-east-1 on-demand, first pricing band; price list 2026-09-11).

Ladders encode on the Basic tier: AVC single-pass HQ with QVBR, no acceleration, normalisation or captions. A job
with ESAM (editorial cue points between segment boundaries) is billed at the Professional tier for every output.
Each output is billed per started second with a 10 s minimum; the muxed audio is part of its variant's output, so it
adds no output of its own.
"""

from math import ceil
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

VIDEO_USD_PER_MINUTE: Final = {"BASIC": (0.0075, 0.015), "PROFESSIONAL": (0.012, 0.024)}
"""AVC at <= 30 fps, single-pass HQ, per output minute and tier: SD (< 720 lines), HD (720-1080 lines)."""
HIGH_FRAME_RATE_FACTOR: Final = 1.25  # > 30 to <= 60 fps (HD: $0.0188 against $0.015)
MIN_BILLED_SECONDS: Final = 10


def _per_minute(ladder: Ladder, *, professional: bool) -> list[float]:
    rates = VIDEO_USD_PER_MINUTE["PROFESSIONAL" if professional else "BASIC"]
    factor = HIGH_FRAME_RATE_FACTOR if ladder.frame_rate.fps > 30.001 else 1.0
    # Landscape is measured vertically, portrait horizontally.
    return [rates[min(r.width, r.height) >= 720] * factor for r in ladder.video]


def encode_cost(ladder: Ladder, duration_ms: int, *, professional: bool = False) -> float:
    """Estimated USD to encode ``duration_ms`` of content with ``ladder`` (per-output 1 s rounding, 10 s minimum);
    ``professional`` for a job with ESAM (:attr:`JobOverrides.professional`)."""
    billed_minutes = max(ceil(duration_ms / 1000), MIN_BILLED_SECONDS) / 60
    return round(sum(_per_minute(ladder, professional=professional)) * billed_minutes, 4)


def encode_cost_per_minute(ladder: Ladder, *, professional: bool = False) -> float:
    """Estimated USD per content-minute (STANDARD ~ $0.0375 on Basic, $0.06 with ESAM)."""
    return encode_cost(ladder, 60_000, professional=professional)
