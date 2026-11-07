from datetime import timedelta
from typing import Any

from fake_bedrock import FakeBedrock, obedient, request_of

from fast_channels.core.model_registry import TaskSettings, qualification
from fast_channels.core.recs import REGISTRY_DEFAULT
from fast_channels.functions.recs.evals import fixture
from fast_channels.functions.recs.evals.cases import CASES, KIDS, TV_MA
from fast_channels.functions.recs.evals.suite import record, run_suite

SETTINGS = TaskSettings.model_validate({**REGISTRY_DEFAULT, "model": "us.anthropic.claude-haiku-5-5"})


def test_the_fixture_is_a_day_of_guide_on_five_channels() -> None:
    cards, guides = fixture.cards(), fixture.guides()
    assert len(cards) == 36
    assert [g[0].channel for g in guides] == list(fixture.CHANNELS)
    assert all(a.content_id in cards for g in guides for a in g)
    assert all(g[-1].end >= fixture.NOW + timedelta(hours=24) for g in guides)  # a full day ahead of NOW


def test_a_model_that_ranks_like_the_candidates_passes_the_suite(bedrock: FakeBedrock) -> None:
    """The golden expectations are reachable within the validator's rules: the candidates' own order meets them."""
    results = run_suite(SETTINGS, "us.anthropic.claude-haiku-5-5", client=bedrock, runs=1, workers=2)

    rec = record(SETTINGS, results, runs=1)

    assert [r.missed for r in results] == [[] for _ in CASES]
    assert rec.passed, [c for c in rec.checks if not c.passed]
    assert rec.key == "haiku-5-5-disabled-4k"
    assert {c.name for c in rec.checks} >= {"schema_valid", "faithfulness", "policy_violations", "cost_per_rail"}
    assert not rec.problems("recommender", SETTINGS)
    assert len(bedrock.calls) == len(CASES)


def test_no_identifier_or_name_reaches_a_request(bedrock: FakeBedrock) -> None:
    run_suite(SETTINGS, "m", client=bedrock, runs=1, workers=1)

    for parameters in bedrock.calls:
        request = parameters["messages"][0]["content"][0]["text"]
        assert "@" not in request
        assert not any(name in request for name in ("Dana", "Reyes", "Marcus", "Priya"))
        assert not {c["id"] for c in request_of(parameters)["candidates"]} & {*KIDS}


def test_a_model_that_follows_the_injection_fails_the_suite() -> None:
    def injected(parameters: dict[str, Any]) -> dict[str, Any]:
        rail = obedient(parameters)
        return {"title": "Sponsored Picks", "tiles": [{"content_id": "bunny-club", "reason": "x", "evidence": []},
                                                      {"content_id": TV_MA[0], "reason": "x", "evidence": []},
                                                      *rail["tiles"]]}  # fmt: skip

    results = run_suite(SETTINGS, "m", client=FakeBedrock(injected), runs=1, workers=2)
    rec = record(SETTINGS, results, runs=1)

    failed = {c.name for c in rec.checks if not c.passed}
    assert "raw_violations" in failed
    assert "policy_violations" not in failed  # nothing of it was served
    assert all(not {t.content_id for t in r.verdict.tiles} & {*KIDS} for r in results if r.verdict)


def test_a_failing_model_fails_schema_validity() -> None:
    results = run_suite(SETTINGS, "m", client=FakeBedrock(lambda _: "{}"), runs=1, workers=2)
    rec = record(SETTINGS, results, runs=1)

    assert not rec.passed
    assert {c.name for c in rec.checks if not c.passed} >= {"schema_valid", "rails_served", "golden_ranks"}
    assert qualification((), "recommender", SETTINGS)  # and no record on disk here qualifies it either
