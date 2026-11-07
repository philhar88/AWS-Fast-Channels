"""Run the describe task's suite and turn it into an eval record (``make eval TASK=understanding.describe``).

Each case is a sample title of the reference catalog (open-licensed films of the Blender Foundation): the signals
that a deployment's analyses gave it (``fixtures/<content_id>.json``, from a live run) and an excerpt of its
pictures. The suite cuts the excerpt from the title's public source with ffmpeg: up to three one-minute windows at
640 by 360, without audio, among them the scenes the moderation labels flagged, cached under ``--cache``. Every case
runs ``runs`` times through the real prompt, the forced tool call and the checks, with the excerpt sent inline (at
most 25 MB). Only Bedrock is called. The checks:

| Check | Pass |
|---|---|
| ``schema_valid`` | calls that called the tool with every field: at least 0.99 |
| ``categories_valid`` | the model's genres that are in the vocabulary (IAB CTV genres): 0.90 |
| ``expected_categories`` | calls whose first three categories (what bidders see) hold an expected one: 0.80 |
| ``no_wrong_categories`` | calls whose categories hold none of the case's clearly wrong ones: 0.90 |
| ``moderation_agreement`` | calls whose own tier is at most one step below the moderation labels': 0.90 |
| ``brand_safety_expected`` | calls whose item tier (the worse of the model's and the labels') is the case's: 0.80 |
| ``moods_valid`` | the model's moods that are in the vocabulary: 0.90 |
| ``cost_per_hour`` | the mean upper-bound cost of an hour-long title (see :func:`upper_cost`): at most $0.50 |
"""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from fast_channels.core.crawl.taxonomy import MAX_GENRES as CONTEXT_CATEGORIES
from fast_channels.core.model_registry import EvalCheck, EvalRecord, TaskSettings, record_key
from fast_channels.core.model_spend import cost
from fast_channels.core.understanding.cost import MAX_FRAMES
from fast_channels.core.understanding.prompt import answer, description, genres, request
from fast_channels.core.understanding.record import TASK, BrandSafety, Mood
from fast_channels.core.understanding.signals import Signals  # noqa: TC001 - pydantic resolves field types at runtime

FIXTURES: Final = Path(__file__).parent / "fixtures"
WINDOW_S: Final = 60
"""Each excerpt window; three make a clip of three minutes (180 frames: the model samples one a second)."""
INLINE_MAX_BYTES: Final = 25 * 1024 * 1024
"""Bedrock's limit on a video sent inline."""
SUITE: Final = "understanding describe conformance v2: the sample titles' signals and excerpts, expected tiers"
THRESHOLDS: Final = {
    "schema_valid": 0.99,
    "categories_valid": 0.90,
    "expected_categories": 0.80,
    "no_wrong_categories": 0.90,
    "moderation_agreement": 0.90,
    "brand_safety_expected": 0.80,
    "moods_valid": 0.90,
}
COST_PER_HOUR_MAX: Final = Decimal("0.50")
FIELDS: Final = ("genres", "keywords", "moods", "brand_safety", "synopsis")


class Expect(BaseModel):
    """What a case's answer must show."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    categories_any: tuple[str, ...]
    """At least one of these Content Taxonomy 3.1 ids among the first three checked categories, which the catalog
    context object carries to bidders."""
    categories_none: tuple[str, ...] = ()
    brand_safety: BrandSafety | None = None
    """The item's tier the title should get: the worse of the model's and the moderation labels'."""
    """Content Taxonomy 3.1 ids that are clearly wrong for the title (Documentary for a fantasy film)."""


class Case(BaseModel):
    """One sample title."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_id: str
    title: str
    source: str
    """The title's public source, from which the excerpt is cut."""
    license: str
    duration_ms: int
    windows_s: tuple[int, ...]
    """Where the excerpt's windows start (each at most :data:`WINDOW_S` long, within the title)."""
    signals: Signals
    expect: Expect

    def windows(self) -> list[tuple[int, int]]:
        """The excerpt's windows as (start, length) in seconds."""
        end = self.duration_ms // 1000
        return [(start, min(WINDOW_S, end - start)) for start in self.windows_s if start < end]

    @property
    def frames(self) -> int:
        """Frames the model samples from the excerpt (one a second)."""
        return min(MAX_FRAMES, sum(length for _, length in self.windows()))


def cases(directory: Path = FIXTURES) -> list[Case]:
    """The suite's cases, by content id."""
    return [Case.model_validate_json(path.read_text()) for path in sorted(directory.glob("*.json"))]


def excerpt(case: Case, cache: Path) -> bytes:
    """The case's excerpt: its windows at 640 by 360, without audio (the model reads pictures only).

    Raises:
        RuntimeError: ffmpeg is missing or failed, or the excerpt is too large to send inline.
    """
    target = cache / f"{case.content_id}.mp4"
    if not target.exists():
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("the suite needs ffmpeg on the PATH to cut the excerpts")
        cache.mkdir(parents=True, exist_ok=True)
        windows = case.windows()
        inputs: list[str] = []
        for start, length in windows:
            inputs += ["-ss", str(start), "-t", str(length), "-i", case.source]
        scaled = "".join(f"[{n}:v]scale=640:360,setsar=1,fps=24[v{n}];" for n in range(len(windows)))
        joined = "".join(f"[v{n}]" for n in range(len(windows)))
        graph = f"{scaled}{joined}concat=n={len(windows)}:v=1:a=0[v]"
        with tempfile.TemporaryDirectory() as scratch:
            out = Path(scratch) / "excerpt.mp4"
            command = ["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]"]
            command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-movflags", "+faststart", str(out)]
            done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 - fixed arguments
            if done.returncode != 0:
                raise RuntimeError(f"ffmpeg failed for {case.content_id}: {done.stderr[-500:]}")
            shutil.move(out, target)
    video = target.read_bytes()
    if len(video) > INLINE_MAX_BYTES:
        raise RuntimeError(f"the excerpt of {case.content_id} is {len(video)} bytes, over the inline limit")
    return video


def upper_cost(model_id: str, usage: Mapping[str, int]) -> Decimal:
    """The cost of a call with every input token at the input price. Amazon Nova reports most of a request's video
    tokens as cache reads, or as cache writes on the first request of a cache's life, which its Price List prices at
    a quarter of the input price and at $0: counted at the input price, this is the most the call can cost."""
    tokens = sum(usage.get(key, 0) for key in ("inputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"))
    return cost(model_id, {**usage, "inputTokens": tokens, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})


@dataclass(slots=True)
class Run:
    """One call of one case."""

    case: str
    run: int
    frames: int
    schema_valid: bool = False
    failure: str | None = None
    raw_moods: list[str] = field(default_factory=list)
    valid_moods: int = 0
    raw_genres: list[str] = field(default_factory=list)
    valid_genres: int = 0
    categories: list[str] = field(default_factory=list)
    """The checked categories, as the item holds them."""
    expected: bool = False
    wrong: bool = False
    agrees: bool = False
    tier: BrandSafety | None = None
    """The item's tier: the worse of the model's and the moderation labels'."""
    tier_expected: bool = False
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Decimal = Decimal(0)
    """The call's upper-bound cost (:func:`upper_cost`)."""
    output: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "case": self.case,
            "run": self.run,
            "frames": self.frames,
            "schema_valid": self.schema_valid,
            "failure": self.failure,
            "raw_genres": self.raw_genres,
            "categories": self.categories,
            "expected": self.expected,
            "wrong": self.wrong,
            "agrees": self.agrees,
            "tier": self.tier,
            "tier_expected": self.tier_expected,
            "usage": self.usage,
            "cost_usd": str(self.cost_usd),
            "output": self.output,
        }


def run_case(case: Case, index: int, *, settings: TaskSettings, model_id: str, client: Any, video: bytes) -> Run:
    """One call of ``case``: the request, the model's answer, its checks."""
    run = Run(case=case.content_id, run=index, frames=case.frames)
    sent = request(
        model_id=model_id,
        video_uri=None,
        bucket_owner=None,
        video_bytes=video,
        signals=case.signals,
        max_tokens=settings.max_tokens,
    )
    try:
        response = client.converse(**sent)
    except Exception as error:  # every failure of a call is a failed run, reported
        run.failure = f"{type(error).__name__}: {error}"[:300]
        return run
    run.usage = {k: int(v) for k, v in (response.get("usage") or {}).items()}
    run.cost_usd = upper_cost(settings.model, run.usage)
    try:
        raw = answer(response)
    except ValueError as error:
        run.failure = str(error)
        return run
    run.output = dict(raw)
    run.schema_valid = all(key in raw for key in FIELDS)
    vocabulary = {name.lower() for name in genres()}
    run.raw_genres = [str(value) for value in raw.get("genres") or []]
    run.valid_genres = sum(value.strip().lower() in vocabulary for value in run.raw_genres)
    run.raw_moods = [str(value) for value in raw.get("moods") or []]
    run.valid_moods = sum(value.strip().lower() in {m.value for m in Mood} for value in run.raw_moods)
    checked = description(raw, moderation_tier=None)
    run.categories = list(checked.cat)
    run.expected = bool(set(checked.cat[:CONTEXT_CATEGORIES]) & set(case.expect.categories_any))
    run.wrong = bool(set(checked.cat) & set(case.expect.categories_none))
    floor = case.signals.moderation_tier or BrandSafety.LOW
    run.agrees = checked.brand_safety is not None and checked.brand_safety.rank >= floor.rank - 1
    run.tier = description(raw, moderation_tier=case.signals.moderation_tier).brand_safety
    run.tier_expected = case.expect.brand_safety is None or run.tier is case.expect.brand_safety
    return run


def run_suite(
    settings: TaskSettings,
    model_id: str,
    *,
    client: Any,
    cache: Path,
    runs: int = 3,
    selected: Sequence[Case] | None = None,
    progress: Callable[[Run], None] | None = None,
) -> list[Run]:
    """Every case ``runs`` times, one call after another (the excerpts are cut first)."""
    chosen = list(selected) if selected is not None else cases()
    videos = {case.content_id: excerpt(case, cache) for case in chosen}
    results: list[Run] = []
    for index in range(runs):
        for case in chosen:
            result = run_case(
                case, index, settings=settings, model_id=model_id, client=client, video=videos[case.content_id]
            )
            results.append(result)
            if progress:
                progress(result)
    return results


def per_hour(results: Sequence[Run]) -> Decimal:
    """The mean upper-bound cost of an hour-long title: each call's cost scaled from its excerpt's frames to the
    model's :data:`~fast_channels.core.understanding.cost.MAX_FRAMES` (which also scales the text, so it errs high)."""
    scaled = [r.cost_usd * MAX_FRAMES / r.frames for r in results if r.frames and r.schema_valid]
    return (sum(scaled, Decimal(0)) / len(scaled)).quantize(Decimal("0.0001")) if scaled else Decimal(0)


def checks(results: Sequence[Run]) -> tuple[EvalCheck, ...]:
    """The record's checks from a suite run."""

    def share(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    values = {
        "schema_valid": share(sum(r.schema_valid for r in results), len(results)),
        "categories_valid": share(sum(r.valid_genres for r in results), sum(len(r.raw_genres) for r in results)),
        "expected_categories": share(sum(r.expected for r in results), len(results)),
        "no_wrong_categories": share(sum(r.schema_valid and not r.wrong for r in results), len(results)),
        "moderation_agreement": share(sum(r.agrees for r in results), len(results)),
        "brand_safety_expected": share(sum(r.schema_valid and r.tier_expected for r in results), len(results)),
        "moods_valid": share(sum(r.valid_moods for r in results), sum(len(r.raw_moods) for r in results)),
    }
    found = [
        EvalCheck(name=name, passed=values[name] >= threshold, value=values[name], threshold=f">= {threshold:.2f}")
        for name, threshold in THRESHOLDS.items()
    ]
    hour = per_hour(results)
    found.append(
        EvalCheck(
            name="cost_per_hour",
            passed=Decimal(0) < hour <= COST_PER_HOUR_MAX,
            value=float(hour),
            threshold=f"<= {COST_PER_HOUR_MAX}",
        )
    )
    return tuple(found)


def record(settings: TaskSettings, results: Sequence[Run], *, runs: int) -> EvalRecord:
    """The eval record of a suite run (passing or not)."""
    found = checks(results)
    return EvalRecord(
        task=TASK,
        key=record_key(settings.model, settings.thinking, settings.max_tokens),
        model=settings.model,
        effort=settings.effort,
        thinking=settings.thinking,
        max_tokens=settings.max_tokens,
        suite=f"{SUITE} ({len({r.case for r in results})} titles)",
        runs=runs,
        passed=all(check.passed for check in found),
        checks=found,
        cost_usd=sum((r.cost_usd for r in results), Decimal(0)).quantize(Decimal("0.000001")),
        recorded_at=datetime.now(UTC),
    )


def write_runs(directory: Path, results: Sequence[Run]) -> Path:
    """The runs as JSON lines, for review."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "runs.jsonl"
    path.write_text("".join(json.dumps(r.summary()) + "\n" for r in results))
    return path
