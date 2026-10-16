import pytest

from fast_channels.core.cost import encode_cost, encode_cost_per_minute
from fast_channels.core.ladder import Ladder, VideoCodec


def test_builtin_costs_match_ladders_md(standard: Ladder, uhd: Ladder) -> None:
    assert encode_cost_per_minute(standard) == pytest.approx(0.56, abs=0.005)  # _review.md: ~$0.56/min at 1080p
    assert encode_cost_per_minute(uhd) == pytest.approx(1.35, abs=0.01)  # ladders.md §4.4: ~$1.35/min


def test_short_outputs_bill_ten_seconds(standard: Ladder) -> None:
    assert encode_cost(standard, 2002) == encode_cost(standard, 10_000) == pytest.approx(0.56 / 6, abs=1e-3)
    assert encode_cost(standard, 10_001) > encode_cost(standard, 10_000)


def test_basic_tier_and_high_frame_rate(standard: Ladder) -> None:
    avc = [r.model_dump() for r in standard.video if r.codec is VideoCodec.AVC]
    basic = Ladder.model_validate(
        standard.model_dump() | {"video": avc, "options": {"loudness_lkfs": None, "acceleration": "DISABLED"}}
    )
    assert basic.pricing_tier() == "BASIC"
    assert encode_cost_per_minute(basic) == pytest.approx(4 * 0.0075 + 4 * 0.015 + 3 * 0.003)
    fast = Ladder.model_validate(basic.model_dump() | {"frame_rate": "59.94"})
    assert encode_cost_per_minute(fast) == pytest.approx((4 * 0.0075 + 4 * 0.015) * 1.25 + 3 * 0.003)


def test_portrait_outputs_are_classed_by_width(standard: Ladder) -> None:
    portrait = [r.model_dump() | {"width": r.height, "height": r.width} for r in standard.video]
    rotated = Ladder.model_construct(
        **(dict(standard) | {"video": tuple(type(standard.video[0]).model_validate(r) for r in portrait)})
    )
    assert encode_cost_per_minute(rotated) == encode_cost_per_minute(standard)
