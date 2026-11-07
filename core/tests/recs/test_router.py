import pytest

from fast_channels.core.recs.router import Personalization, Reason, Strategy, holdout_bucket, route

IN_HOLDOUT = next(f"vid-{i}" for i in range(1000) if holdout_bucket(f"vid-{i}") < 10)
OUTSIDE = next(f"vid-{i}" for i in range(1000) if holdout_bucket(f"vid-{i}") >= 10)


def decide(vid: str = OUTSIDE, **overrides: object) -> tuple[Strategy, Reason]:
    request: dict[str, object] = {
        "vid": vid,
        "kill_switch": False,
        "kids": False,
        "personalization": Personalization.ALLOWED,
        "first_session": False,
    }
    return route(**{**request, **overrides})  # type: ignore[arg-type]


def test_an_adult_with_consent_gets_the_model_rail() -> None:
    assert decide() == (Strategy.LLM, Reason.MODEL)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"kill_switch": True, "kids": True}, (Strategy.BASELINE, Reason.KILL_SWITCH)),
        ({"kids": True, "personalization": Personalization.OPTED_OUT}, (Strategy.KIDS, Reason.KIDS)),
        ({"personalization": Personalization.OPTED_OUT}, (Strategy.COHORT, Reason.OPTED_OUT)),
        ({"personalization": Personalization.UNAVAILABLE}, (Strategy.COHORT, Reason.NO_CONSENT)),
        ({"first_session": True}, (Strategy.COHORT, Reason.FIRST_SESSION)),
    ],
)
def test_the_router_decides_before_any_model_call(overrides: dict[str, object], expected: tuple[object, ...]) -> None:
    assert decide(**overrides) == expected


def test_the_holdout_gets_the_baseline_even_in_its_first_session() -> None:
    assert decide(IN_HOLDOUT, first_session=True) == (Strategy.BASELINE, Reason.HOLDOUT)
    assert decide(IN_HOLDOUT, holdout_percent=0) == (Strategy.LLM, Reason.MODEL)


def test_the_holdout_is_about_ten_percent_and_stable() -> None:
    buckets = [holdout_bucket(f"v{i}") for i in range(10_000)]
    assert 0.09 < sum(b < 10 for b in buckets) / len(buckets) < 0.11
    assert holdout_bucket("v1") == holdout_bucket("v1")
    assert holdout_bucket("v1", seed="rail-v2") != holdout_bucket("v1") or holdout_bucket("v2", seed="rail-v2") != (
        holdout_bucket("v2")
    )
