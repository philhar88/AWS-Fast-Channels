import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.model_registry import TaskSettings
from fast_channels.core.understanding import REGISTRY_DEFAULT, Analysis
from fast_channels.core.understanding.prompt import TOOL
from fast_channels.core.understanding.signals import Signals
from fast_channels.functions.understanding.evals import suite

SETTINGS = TaskSettings.model_validate({**REGISTRY_DEFAULT, "model": "us.amazon.nova-2-lite-v1:0"})
SAMPLE_CATALOG = Path(__file__).resolve().parents[3] / "assets" / "sample-catalog.json"
USAGE = {"inputTokens": 900, "outputTokens": 150, "cacheReadInputTokens": 40_000, "cacheWriteInputTokens": 17_000}


def case(**changes: Any) -> suite.Case:
    signals = Signals(
        duration_ms=600_000,
        analyses=(Analysis.TRANSCRIPT, Analysis.MODERATION),
        language="en",
        transcript="A robot walks through a ruined city.",
        moderation={"Violence": 92.0},
    )
    base: dict[str, Any] = {
        "content_id": "film",
        "title": "Film",
        "source": "https://example.com/film.mp4",
        "license": "CC BY 3.0",
        "duration_ms": 600_000,
        "windows_s": (60, 300, 540),
        "signals": signals,
        "expect": suite.Expect(categories_any=("652",), categories_none=("332", "641")),
    }
    return suite.Case.model_validate({**base, **changes})


class Model:
    """A Converse client that answers with one tool call."""

    def __init__(self, answer: dict[str, Any] | None, usage: dict[str, int] = USAGE) -> None:
        self.answer, self.usage, self.calls = answer, usage, []

    def converse(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        content = [{"toolUse": {"toolUseId": "t", "name": TOOL, "input": self.answer}}] if self.answer else []
        return {
            "output": {"message": {"role": "assistant", "content": content}},
            "stopReason": "tool_use" if self.answer else "end_turn",
            "usage": self.usage,
        }


GOOD = {
    "genres": ["Sci-Fi", "Action"],
    "keywords": ["robots", "city"],
    "moods": ["tense", "dark"],
    "brand_safety": "medium",
    "synopsis": "A robot crosses a ruined city.",
}


def test_the_windows_stay_inside_the_title() -> None:
    short = case(duration_ms=52_000, windows_s=(0, 60))

    assert case().windows() == [(60, 60), (300, 60), (540, 60)]
    assert case().frames == 180
    assert short.windows() == [(0, 52)]
    assert short.frames == 52


def test_cache_reads_and_writes_count_as_input_in_the_upper_cost() -> None:
    """With a cache point, Nova reported video tokens as cache reads and writes (a quarter of the price, and $0)."""
    upper = suite.upper_cost("us.amazon.nova-2-lite-v1:0", USAGE)

    assert upper == (Decimal(57_900) * Decimal("0.33") + Decimal(150) * Decimal("2.75")) / 1_000_000


def test_a_model_that_answers_well_passes(tmp_path: Path) -> None:
    model = Model(GOOD)

    results = [
        suite.run_case(case(), n, settings=SETTINGS, model_id="arn:p", client=model, video=b"mp4") for n in (0, 1)
    ]
    record = suite.record(SETTINGS, results, runs=2)

    assert record.passed, [c for c in record.checks if not c.passed]
    assert record.key == "amazon.nova-2-lite-v1-0-disabled-4k"
    assert not record.problems("understanding.describe", SETTINGS)
    sent = model.calls[0]["messages"][0]["content"]
    assert sent[0] == {"video": {"format": "mp4", "source": {"bytes": b"mp4"}}}
    assert model.calls[0]["modelId"] == "arn:p"
    hour = next(c for c in record.checks if c.name == "cost_per_hour")
    assert hour.value == pytest.approx(float(results[0].cost_usd) * 960 / 180, abs=1e-4)
    assert json.loads(suite.write_runs(tmp_path, results).read_text().splitlines()[0])["case"] == "film"


def test_genres_outside_the_vocabulary_are_invalid_and_wrong_ones_count() -> None:
    results = [
        suite.run_case(case(), 0, settings=SETTINGS, model_id="p", client=Model({**GOOD, "genres": g}), video=b"")
        for g in (["sci-fi"], ["Robots", "Ruins"], ["Animation", "Documentary", "Sci-Fi"], ["Drama", "Romance"])
    ]

    assert [(r.valid_genres, r.expected, r.wrong) for r in results] == [
        (1, True, False),  # names are compared without case
        (0, False, False),
        (3, True, True),  # Science Fiction is there, but so are two wrong ones
        (2, False, False),
    ]
    checks = {c.name: c for c in suite.checks(results)}
    assert checks["categories_valid"].value == pytest.approx(6 / 8)
    assert (checks["expected_categories"].value, checks["no_wrong_categories"].value) == (0.5, 0.75)
    assert not any(checks[n].passed for n in ("categories_valid", "expected_categories", "no_wrong_categories"))


def test_the_expected_genre_must_be_among_the_three_categories_bidders_see() -> None:
    late = Model({**GOOD, "genres": ["Drama", "Romance", "Thriller", "Sci-Fi"]})  # a fourth genre is dropped

    result = suite.run_case(case(), 0, settings=SETTINGS, model_id="p", client=late, video=b"")

    assert result.categories == ["647", "326", "700"] and not result.expected


def test_a_tier_far_below_the_moderation_labels_disagrees() -> None:
    """Violence at 92 is the high tier: medium agrees (one step below), low does not."""
    agree, disagree = (
        suite.run_case(case(), 0, settings=SETTINGS, model_id="p", client=Model({**GOOD, "brand_safety": t}), video=b"")
        for t in ("medium", "low")
    )

    assert (agree.agrees, disagree.agrees) == (True, False)


def test_a_call_without_the_tool_fails_every_check() -> None:
    results = [suite.run_case(case(), 0, settings=SETTINGS, model_id="p", client=Model(None), video=b"")]

    record = suite.record(SETTINGS, results, runs=1)

    assert results[0].failure and "did not call" in results[0].failure
    assert not record.passed
    failed = {c.name for c in record.checks if not c.passed}
    assert failed >= {"schema_valid", "expected_categories", "no_wrong_categories", "cost_per_hour"}


def test_the_fixtures_are_the_sample_titles() -> None:
    titles = {t["content_id"]: t for t in json.loads(SAMPLE_CATALOG.read_text())["titles"]}

    cases = suite.cases()

    assert {c.content_id for c in cases} == set(titles)
    for found in cases:
        title = titles[found.content_id]
        assert (found.title, found.duration_ms) == (title["title"], title["duration_ms"])
        assert found.source == title["mezzanine_uri"]
        assert found.license == title["license"]
        assert abs(found.signals.duration_ms - title["duration_ms"]) <= 2_000  # the encode's, to a frame or two
        assert found.windows() and found.frames <= 180
        assert found.expect.categories_any
