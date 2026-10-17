import pytest

from fast_channels.core.cost import (
    HIGH_FRAME_RATE_FACTOR,
    MIN_BILLED_SECONDS,
    VIDEO_USD_PER_MINUTE,
    encode_cost,
    encode_cost_per_minute,
)
from fast_channels.core.ladder import Ladder, VideoRendition

SD, HD = VIDEO_USD_PER_MINUTE


def test_basic_tier_prices() -> None:
    assert (SD, HD) == (0.0075, 0.015)  # AVC <= 30 fps, Basic tier, us-east-1, per output minute
    assert (HIGH_FRAME_RATE_FACTOR, MIN_BILLED_SECONDS) == (1.25, 10)


def test_standard_costs_under_four_cents_a_minute(standard: Ladder) -> None:
    assert encode_cost_per_minute(standard) == pytest.approx(HD + HD + SD) == pytest.approx(0.0375)
    assert encode_cost(standard, 3_600_000) == pytest.approx(2.25)  # per content hour


def test_short_outputs_bill_ten_seconds(standard: Ladder) -> None:
    assert encode_cost(standard, 2002) == encode_cost(standard, 10_000) == pytest.approx(0.0375 / 6, abs=1e-4)
    assert encode_cost(standard, 10_001) > encode_cost(standard, 10_000)  # per started second
    assert encode_cost(standard, 60_001) == pytest.approx(0.0375 * 61 / 60, abs=1e-4)


def test_outputs_are_classed_by_lines(standard: Ladder) -> None:
    def ladder(*sizes: tuple[int, int]) -> Ladder:
        video = [
            VideoRendition(name=f"avc-{h}p", width=w, height=h, qvbr=7, max_kbps=1000, hrd_kbps=1000) for w, h in sizes
        ]
        return Ladder.model_validate(standard.model_dump() | {"video": video})

    assert encode_cost_per_minute(ladder((960, 540))) == pytest.approx(SD)
    assert encode_cost_per_minute(ladder((1280, 720))) == pytest.approx(HD)  # 720 lines is HD
    assert encode_cost_per_minute(ladder((1920, 1080), (1280, 720), (960, 540), (640, 360))) == pytest.approx(
        2 * HD + 2 * SD
    )


def test_muxed_audio_adds_no_output(standard: Ladder) -> None:
    louder = Ladder.model_validate(standard.model_dump() | {"audio": {"kbps": 320}})
    assert encode_cost_per_minute(louder) == encode_cost_per_minute(standard)


def test_high_frame_rate(standard: Ladder) -> None:
    fast = Ladder.model_validate(standard.model_dump() | {"frame_rate": "59.94"})
    assert encode_cost_per_minute(fast) == pytest.approx(0.0375 * 1.25, abs=1e-4)
    assert encode_cost_per_minute(Ladder.model_validate(standard.model_dump() | {"frame_rate": "30"})) == 0.0375


def test_portrait_outputs_are_classed_by_width(standard: Ladder) -> None:
    portrait = [r.model_dump() | {"width": r.height, "height": r.width} for r in standard.video]
    rotated = Ladder.model_construct(
        **(dict(standard) | {"video": tuple(VideoRendition.model_construct(**r) for r in portrait)})
    )
    assert encode_cost_per_minute(rotated) == encode_cost_per_minute(standard)
