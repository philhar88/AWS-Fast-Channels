"""What understanding a title costs: list prices per content minute (us-east-1, AWS Price List API, 2026-11-07).

Every analysis bills by the minute of the title (Rekognition's segment detection per segment type), the proxy by the
minute of its encode, and the describe step by tokens: Amazon Nova 2 Lite reads a video of 16 minutes or less at one
frame per second and a longer one as 960 frames, 256 tokens a frame (measured at 640 by 360). A title is analysed
once, so this is a one-time cost per content hour, not a running one.
"""

from collections.abc import Iterable
from decimal import Decimal
from typing import Final

from fast_channels.core.understanding.record import Analysis

PER_MINUTE: Final = {
    Analysis.LABELS: Decimal("0.10"),
    Analysis.MODERATION: Decimal("0.10"),
    Analysis.SHOTS: Decimal("0.05"),
    Analysis.TECHNICAL_CUES: Decimal("0.05"),
    Analysis.TRANSCRIPT: Decimal("0.006"),
}
"""US dollars per content minute: Rekognition Video archived analysis (segment detection per type), Transcribe
batch ($0.0001 a second)."""
PROXY_PER_MINUTE: Final = Decimal("0.0075")
"""MediaConvert Basic tier, SD AVC, single pass."""
FRAME_TOKENS: Final = 256
"""Amazon Nova 2 Lite's tokens for a 640 by 360 frame (46,022 for the suite's 180-frame excerpts, 2026-11-07)."""
MAX_FRAMES: Final = 960
TEXT_TOKENS: Final = 4_500
"""The instructions, the signals and a full transcript excerpt (8,000 characters), at most: the sample titles' text
was 2,000 to 2,500 tokens."""
OUTPUT_TOKENS: Final = 400
NOVA_INPUT_PER_MILLION: Final = Decimal("0.33")
NOVA_OUTPUT_PER_MILLION: Final = Decimal("2.75")
"""Amazon Nova 2 Lite in a US inference profile."""


def video_tokens(duration_ms: int) -> int:
    """Tokens of the video a model call reads: one frame a second up to :data:`MAX_FRAMES`."""
    return min(MAX_FRAMES, max(1, round(duration_ms / 1000))) * FRAME_TOKENS


def estimate(duration_ms: int, analyses: Iterable[Analysis]) -> Decimal:
    """List-price cost of understanding a title of ``duration_ms`` with ``analyses`` (US dollars)."""
    minutes = Decimal(duration_ms) / 60_000
    per_minute = sum((PER_MINUTE[a] for a in set(analyses)), Decimal(0)) + PROXY_PER_MINUTE
    tokens_in = video_tokens(duration_ms) + TEXT_TOKENS
    model = (tokens_in * NOVA_INPUT_PER_MILLION + OUTPUT_TOKENS * NOVA_OUTPUT_PER_MILLION) / 1_000_000
    return (minutes * per_minute + model).quantize(Decimal("0.0001"))


def per_hour(analyses: Iterable[Analysis]) -> Decimal:
    """Cost per content hour (one hour-long title) with ``analyses``."""
    return estimate(3_600_000, analyses).quantize(Decimal("0.01"))
