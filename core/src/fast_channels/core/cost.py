"""MediaConvert encode cost estimates (us-east-1 on-demand, first pricing tier; ladders.md §2.6).

Each output is billed per started second with a 10 s minimum; the tier comes from :meth:`Ladder.pricing_tier`.
"""

from math import ceil
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

# USD per output-minute at <= 30 fps for (SD < 720 lines, HD 720-1080, 4K > 1080-2160), keyed (codec, tier).
VIDEO_USD_PER_MINUTE: Final[dict[tuple[str, str], tuple[float, float, float]]] = {
    ("avc", "BASIC"): (0.0075, 0.015, 0.030),
    ("avc", "PROFESSIONAL"): (0.012, 0.024, 0.048),
    ("hevc", "PROFESSIONAL"): (0.024, 0.048, 0.096),
    ("av1", "PROFESSIONAL"): (0.042, 0.084, 0.168),
}
AUDIO_USD_PER_MINUTE: Final = {"BASIC": 0.003, "PROFESSIONAL": 0.00468}
NORMALIZATION_USD_PER_MINUTE: Final = 0.002  # audio normalisation add-on, per audio output
HIGH_FRAME_RATE_FACTOR: Final = 1.25  # > 30 to <= 60 fps (AV1 table: $0.0525/$0.105/$0.210)
MIN_BILLED_SECONDS: Final = 10


def _resolution_class(width: int, height: int) -> int:
    lines = min(width, height)  # landscape is measured vertically, portrait horizontally
    return 0 if lines < 720 else 1 if lines <= 1080 else 2


def _per_minute(ladder: Ladder) -> list[float]:
    tier = ladder.pricing_tier()
    rate = HIGH_FRAME_RATE_FACTOR if ladder.frame_rate.fps > 30.001 else 1.0
    video = [VIDEO_USD_PER_MINUTE[r.codec, tier][_resolution_class(r.width, r.height)] * rate for r in ladder.video]
    audio_rate = AUDIO_USD_PER_MINUTE[tier]
    if ladder.options.loudness_lkfs is not None:
        audio_rate += NORMALIZATION_USD_PER_MINUTE
    return video + [audio_rate] * len(ladder.audio)


def encode_cost(ladder: Ladder, duration_ms: int) -> float:
    """Estimated USD to encode ``duration_ms`` of content with ``ladder`` (per-output 1 s rounding, 10 s minimum)."""
    billed_minutes = max(ceil(duration_ms / 1000), MIN_BILLED_SECONDS) / 60
    return round(sum(_per_minute(ladder)) * billed_minutes, 4)


def encode_cost_per_minute(ladder: Ladder) -> float:
    """Estimated USD per content-minute (STANDARD ~ $0.56, UHD ~ $1.36)."""
    return encode_cost(ladder, 60_000)
