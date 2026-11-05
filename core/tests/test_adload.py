"""Ad load by tier: the policy of a tier, which breaks follow it, and λ (ADR-0037)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fast_channels.core.adload import (
    C_INS,
    CONTROL,
    CONTROL_TIER,
    LAMBDA_CAP,
    AdLoad,
    AdLoadPolicy,
    AdLoadReason,
    ad_load,
    slot_lambdas,
)
from fast_channels.core.pds.models import Mode

LIGHT = AdLoadPolicy(max_ads=1, bar_cpm=Decimal(20))
TIERS = {"light": LIGHT, "flat": AdLoadPolicy()}


def test_lambda_is_the_bar_less_an_insertion() -> None:
    assert LIGHT.lambda_usd == Decimal("0.0195")
    assert AdLoadPolicy(bar_cpm=Decimal("0.2")).lambda_usd == 0  # a bar under c_ins prices nothing
    assert CONTROL.lambda_usd == 0
    assert slot_lambdas(3, LIGHT) == (Decimal("0.0195"),) * 3


def test_a_sold_ad_beats_lambda_exactly_when_its_accepted_cpm_beats_the_bar() -> None:
    for cpm in (Decimal("19.99"), Decimal(20), Decimal("20.01")):
        net = cpm / 1000 - C_INS
        assert (net - LIGHT.lambda_usd > 0) == (cpm > LIGHT.bar_cpm)


def test_lambda_never_decreases_as_the_bar_rises() -> None:
    lambdas = [AdLoadPolicy(bar_cpm=Decimal(cents) / 100).lambda_usd for cents in range(0, 2051, 25)]
    assert lambdas == sorted(lambdas)
    assert lambdas[0] == 0


def test_lambda_is_capped() -> None:
    assert AdLoadPolicy(bar_cpm=Decimal("20.5")).lambda_usd == LAMBDA_CAP
    with pytest.raises(ValidationError, match="λ_cap"):
        AdLoadPolicy(bar_cpm=Decimal("20.51"))


@pytest.mark.parametrize("fields", [{"max_ads": 0}, {"max_ads": 17}, {"bar_cpm": -1}, {"floor": 1}])
def test_a_policy_outside_its_bounds_is_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AdLoadPolicy.model_validate(fields)


def test_only_no_cap_and_no_bar_is_the_control_plan() -> None:
    assert CONTROL.is_control
    assert AdLoadPolicy(bar_cpm=Decimal("0.5")).is_control  # λ is 0
    assert not AdLoadPolicy(max_ads=4).is_control
    assert not LIGHT.is_control


@pytest.mark.parametrize("mode", [Mode.FULL, Mode.PSEUDONYMOUS])
def test_a_personalized_break_follows_its_viewers_tier(mode: Mode) -> None:
    assert ad_load("light", mode, probe=False, tiers=TIERS) == AdLoad("light", AdLoadReason.TIER, LIGHT)


@pytest.mark.parametrize("mode", [Mode.CONTEXTUAL, Mode.SAFE, Mode.KIDS, None])
def test_every_other_break_keeps_the_control_plan(mode: Mode | None) -> None:
    assert ad_load("light", mode, probe=False, tiers=TIERS) == AdLoad(CONTROL_TIER, AdLoadReason.EXEMPT, CONTROL)


def test_a_probe_keeps_the_control_plan() -> None:
    assert ad_load("light", Mode.FULL, probe=True, tiers=TIERS).reason is AdLoadReason.EXEMPT


@pytest.mark.parametrize("tier", [None, "standard", "holdout", "unknown", "flat"])
def test_no_tier_a_control_tier_or_one_without_a_policy_is_the_control_plan(tier: str | None) -> None:
    assert ad_load(tier, Mode.FULL, probe=False, tiers=TIERS) == AdLoad(CONTROL_TIER, AdLoadReason.CONTROL, CONTROL)


def test_a_control_tier_stays_the_control_plan_even_if_priced() -> None:
    priced = {"holdout": LIGHT, "standard": LIGHT}
    assert ad_load("holdout", Mode.FULL, probe=False, tiers=priced).rules == CONTROL


def test_the_guardrail_keeps_the_tier_and_names_itself() -> None:
    guarded = AdLoad("light", AdLoadReason.TIER, LIGHT).guarded()
    assert guarded == AdLoad("light", AdLoadReason.MIN_LOAD, LIGHT)
