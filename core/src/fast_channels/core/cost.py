"""MediaConvert encode cost estimates (us-east-1 on-demand, Basic tier, first pricing band; price list 2026-09-11).

Every ladder encodes on the Basic tier: AVC single-pass HQ with QVBR, no ESAM, acceleration, normalisation or
captions. Each output is billed per started second with a 10 s minimum; the muxed audio is part of its variant's
output, so it adds no output of its own.
"""

from math import ceil
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

VIDEO_USD_PER_MINUTE: Final = (0.0075, 0.015)
"""AVC at <= 30 fps, Basic tier, per output minute: SD (< 720 lines), HD (720-1080 lines)."""
HIGH_FRAME_RATE_FACTOR: Final = 1.25  # > 30 to <= 60 fps (HD: $0.0188 against $0.015)
MIN_BILLED_SECONDS: Final = 10


def _per_minute(ladder: Ladder) -> list[float]:
    rate = HIGH_FRAME_RATE_FACTOR if ladder.frame_rate.fps > 30.001 else 1.0
    # Landscape is measured vertically, portrait horizontally.
    return [VIDEO_USD_PER_MINUTE[min(r.width, r.height) >= 720] * rate for r in ladder.video]


def encode_cost(ladder: Ladder, duration_ms: int) -> float:
    """Estimated USD to encode ``duration_ms`` of content with ``ladder`` (per-output 1 s rounding, 10 s minimum)."""
    billed_minutes = max(ceil(duration_ms / 1000), MIN_BILLED_SECONDS) / 60
    return round(sum(_per_minute(ladder)) * billed_minutes, 4)


def encode_cost_per_minute(ladder: Ladder) -> float:
    """Estimated USD per content-minute (STANDARD ~ $0.0375, $2.25 per content hour)."""
    return encode_cost(ladder, 60_000)
