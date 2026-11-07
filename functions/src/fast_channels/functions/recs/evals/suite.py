"""Run the recommender suite and turn it into an eval record (``make eval TASK=recommender``).

Every case runs ``runs`` times through the real prompt, the real Converse request and the validator, on the fixture
(:mod:`.fixture`). One call warms the cached catalog prefix before the rest fan out, as the cohort run does in a
deployment. The checks:

| Check | Pass |
|---|---|
| ``schema_valid`` | the share of calls that returned a rail in the output schema, at least 0.99 |
| ``rails_served`` | the share of calls whose rail passed the validator with enough tiles, at least 0.95 |
| ``faithfulness`` | the share of the model's tiles whose reason and evidence held unchanged, at least 0.85 |
| ``policy_violations`` | problems in the served rails, found by an independent audit: 0 |
| ``raw_violations`` | policy problems in the model's output per tile (the validator removed them), at most 0.05 |
| ``golden_ranks`` | the share of the cases' expectations met, at least 0.80 |
| ``cost_per_rail`` | mean list-price cost of a call, at most $0.002 |
| ``latency_p50_s`` | median Converse latency, at most 10 s |
| ``prompt_audit`` | identifiers, names or commercial words found in a request block: 0 |
| ``judge_faithfulness`` | with ``--judge N``: the share of sampled tiles an Opus judge found faithful, at least 0.80 |
"""

import json
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from fast_channels.core.model_registry import EvalCheck, EvalRecord, TaskSettings, record_key
from fast_channels.core.model_spend import cost
from fast_channels.core.recs import TASK
from fast_channels.core.recs.candidates import Candidate, Cohort, Taste, candidates, catalog_cards
from fast_channels.core.recs.prompt import PROMPT_VERSION, catalog_block, request_block
from fast_channels.core.recs.rail import within
from fast_channels.core.recs.validate import (
    BLOCKED,
    PII,
    REASON_MAX,
    Entities,
    RailTile,
    Verdict,
    audit_prompt,
    unsupported,
    validate,
)
from fast_channels.functions.recs import model
from fast_channels.functions.recs.evals import fixture
from fast_channels.functions.recs.evals.cases import CASES, Case

if TYPE_CHECKING:
    from fast_channels.core.recs.cards import Card

SUITE: Final = f"recommender conformance v1 ({PROMPT_VERSION}): {len(CASES)} golden cases on a 36-title fixture"
JUDGE_MODEL: Final = "us.anthropic.claude-opus-5-5"


@dataclass(slots=True)
class Run:
    """One call of one case."""

    case: str
    run: int
    schema_valid: bool = False
    failure: str | None = None
    verdict: Verdict | None = None
    model_tiles: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int | None = None
    met: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    served_problems: list[str] = field(default_factory=list)
    prompt_hits: list[str] = field(default_factory=list)
    output: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        tiles = [t.model_dump() for t in self.verdict.tiles] if self.verdict else []
        return {
            "case": self.case,
            "run": self.run,
            "schema_valid": self.schema_valid,
            "failure": self.failure,
            "served": bool(self.verdict and self.verdict.valid),
            "tiles": tiles,
            "dropped": dict(self.verdict.dropped) if self.verdict else {},
            "violations": dict(self.verdict.violations) if self.verdict else {},
            "faithful": f"{self.verdict.faithful}/{self.verdict.checked}" if self.verdict else None,
            "cost_usd": str(self.cost_usd),
            "latency_ms": self.latency_ms,
            "missed": self.missed,
            "served_problems": self.served_problems,
            "output": self.output,
        }


@dataclass(frozen=True, slots=True)
class Context:
    """What every case shares: the fixture's cards and guides, and the catalog block of each rating ceiling."""

    cards: dict[str, Card]
    guides: list[list[Any]]
    catalogs: dict[str, str]


def context() -> Context:
    """The fixture, and its catalog blocks as a deployment shows them: no kids channel titles, one block per ceiling."""
    cards, guides = fixture.cards(), fixture.guides()
    ceilings = {case.facts.rating_ceiling for case in CASES}
    catalogs = {
        ceiling: catalog_block(
            catalog_cards(cards, guides, fixture.NOW, ceiling=ceiling, child_directed={fixture.KIDS_CHANNEL})
        )
        for ceiling in ceilings
    }
    return Context(cards=cards, guides=guides, catalogs=catalogs)


def case_candidates(case: Case, ctx: Context) -> list[Candidate]:
    return candidates(ctx.cards, ctx.guides, fixture.NOW, case.facts, child_directed={fixture.KIDS_CHANNEL})


def audit_served(tiles: Sequence[RailTile], allowed: Sequence[Candidate], ctx: Context, case: Case) -> list[str]:
    """Problems of a served rail, checked independently of the validator's bookkeeping."""
    ids = {c.content_id: c for c in allowed}
    entities = Entities(ctx.cards.values(), {c.channel for c in allowed})
    problems = []
    for tile in tiles:
        card = ctx.cards.get(tile.content_id)
        if tile.content_id not in ids or card is None:
            problems.append(f"{tile.content_id}: not a candidate")
            continue
        if not within(card.rating, case.facts.rating_ceiling):
            problems.append(f"{tile.content_id}: rated {card.rating}")
        if BLOCKED.search(tile.reason) or any(p.search(tile.reason) for p in PII) or len(tile.reason) > REASON_MAX:
            problems.append(f"{tile.content_id}: reason {tile.reason!r}")
        if any(s.lower() in tile.reason.lower() for s in case.forbidden if s):
            problems.append(f"{tile.content_id}: leaks a forbidden string")
        if claims := unsupported(tile.reason, card, tile.channel, case.facts, entities):
            problems.append(f"{tile.content_id}: unsupported {claims}")
    if len({t.content_id for t in tiles}) != len(tiles):
        problems.append("a title appears twice")
    return problems


def run_case(case: Case, index: int, *, settings: TaskSettings, model_id: str, client: Any, ctx: Context) -> Run:
    """One call of ``case``: prompt, Converse, validator, expectations."""
    run = Run(case=case.name, run=index)
    allowed = case_candidates(case, ctx)
    request = request_block(case.facts, allowed)
    run.prompt_hits = audit_prompt(request, case.forbidden)
    try:
        catalog = ctx.catalogs[case.facts.rating_ceiling]
        rail = model.call(settings, model_id, catalog=catalog, request=request, client=client)
    except model.ModelFailure as failure:
        run.failure = f"{failure.reason}: {failure.detail}"
        run.cost_usd = failure.cost_usd
        return run
    run.schema_valid = True
    run.cost_usd, run.latency_ms = rail.cost_usd, rail.latency_ms
    run.output = rail.output.model_dump(mode="json")
    run.model_tiles = len(rail.output.tiles)
    verdict = validate(run.output, candidates=allowed, cards=ctx.cards, facts=case.facts, forbidden=case.forbidden)
    run.verdict = verdict
    run.served_problems = audit_served(verdict.tiles, allowed, ctx, case)
    for expectation in case.expect:
        (run.met if expectation.met(verdict.tiles, ctx.cards, case.facts) else run.missed).append(
            expectation.describe()
        )
    return run


def run_suite(
    settings: TaskSettings,
    model_id: str,
    *,
    client: Any,
    runs: int = 3,
    workers: int = 4,
    cases: Sequence[Case] = CASES,
    progress: Callable[[Run], None] | None = None,
) -> list[Run]:
    """Every case ``runs`` times. The first call of each catalog block runs alone, so that it writes the cached prefix
    the others read; the rest fan out."""
    ctx = context()
    call = partial(run_case, settings=settings, model_id=model_id, client=client, ctx=ctx)
    jobs = [(case, index) for index in range(runs) for case in cases]
    warm = list({case.facts.rating_ceiling: (case, index) for case, index in reversed(jobs)}.values())
    results: list[Run] = []
    for job in warm:
        results.append(call(*job))
        if progress:
            progress(results[-1])
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(lambda job: call(*job), [job for job in jobs if job not in warm]):
            results.append(result)
            if progress:
                progress(result)
    return results


_JUDGE_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["content_id", "faithful"],
                "properties": {"content_id": {"type": "string"}, "faithful": {"type": "boolean"}},
            },
        }
    },
}
_JUDGE_RUBRIC: Final = (
    "You audit the reasons of a TV recommendation rail. A reason is faithful when every fact it states about the "
    "title or the person is present in the input: the title's card, or the person's listed genres and moods (or, "
    "for a cohort, its daypart). Taste words such as 'you like' are fine when the genre or mood is listed. Judge each "
    "tile; return only the JSON object."
)


def judge(results: Sequence[Run], sample: int, *, client: Any, model_id: str = JUDGE_MODEL) -> tuple[float, Decimal]:
    """The share of tiles an Opus judge finds faithful, over the served tiles of the first ``sample`` served rails;
    and the judge's cost."""
    ctx, cases = context(), {c.name: c for c in CASES}
    judged = faithful = 0
    spent = Decimal(0)
    for result in [r for r in results if r.verdict and r.verdict.valid][:sample]:
        assert result.verdict is not None
        case = cases[result.case]
        facts = case.facts.model_dump(mode="json")
        tiles = [
            {"content_id": t.content_id, "reason": t.reason, "card": ctx.cards[t.content_id].prompt_fields()}
            for t in result.verdict.tiles
        ]
        response = client.converse(
            modelId=model_id,
            system=[{"text": _JUDGE_RUBRIC}],
            messages=[{"role": "user", "content": [{"text": json.dumps({"audience": facts, "tiles": tiles})}]}],
            inferenceConfig={"maxTokens": 4000},
            outputConfig={
                "effort": "low",
                "textFormat": {
                    "type": "json_schema",
                    "structure": {"jsonSchema": {"schema": json.dumps(_JUDGE_SCHEMA), "name": "verdicts"}},
                },
            },
        )
        spent += cost(model_id, response.get("usage") or {})
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        verdicts = json.loads(text)["verdicts"]
        judged += len(verdicts)
        faithful += sum(bool(v["faithful"]) for v in verdicts)
    return (faithful / judged if judged else 0.0), spent


def _check(name: str, value: float, passed: bool, threshold: str) -> EvalCheck:
    return EvalCheck(name=name, passed=passed, value=round(value, 4), threshold=threshold)


def checks(results: Sequence[Run], judged: float | None = None) -> tuple[EvalCheck, ...]:
    """The record's checks over every call."""
    calls = len(results) or 1
    verdicts = [r.verdict for r in results if r.verdict is not None]
    checked = sum(v.checked for v in verdicts)
    model_tiles = sum(r.model_tiles for r in results)
    violations = sum(sum(v.violations.values()) for v in verdicts)
    expectations = sum(len(r.met) + len(r.missed) for r in results)
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    served_problems = sum(len(r.served_problems) for r in results)
    prompt_hits = sum(len(r.prompt_hits) for r in results)
    cost_per_rail = float(sum((r.cost_usd for r in results), Decimal(0))) / calls
    measured: list[tuple[str, float, str, Callable[[float], bool]]] = [
        ("schema_valid", sum(r.schema_valid for r in results) / calls, ">= 0.99", lambda v: v >= 0.99),
        ("rails_served", sum(v.valid for v in verdicts) / calls, ">= 0.95", lambda v: v >= 0.95),
        ("faithfulness", sum(v.faithful for v in verdicts) / (checked or 1), ">= 0.85", lambda v: v >= 0.85),
        ("policy_violations", served_problems, "== 0", lambda v: v == 0),
        ("raw_violations", violations / (model_tiles or 1), "<= 0.05", lambda v: v <= 0.05),
        ("golden_ranks", sum(len(r.met) for r in results) / (expectations or 1), ">= 0.80", lambda v: v >= 0.80),
        ("cost_per_rail", cost_per_rail, "<= 0.002", lambda v: v <= 0.002),
        ("latency_p50_s", statistics.median(latencies) / 1000 if latencies else 0.0, "<= 10", lambda v: v <= 10),
        ("prompt_audit", prompt_hits, "== 0", lambda v: v == 0),
    ]
    if judged is not None:
        measured.append(("judge_faithfulness", judged, ">= 0.80", lambda v: v >= 0.80))
    return tuple(_check(name, value, test(value), threshold) for name, value, threshold, test in measured)


def record(
    settings: TaskSettings,
    results: Sequence[Run],
    *,
    runs: int,
    judged: float | None = None,
    extra_cost: Decimal = Decimal(0),
) -> EvalRecord:
    """The eval record of a suite run (passing or not)."""
    found = checks(results, judged)
    return EvalRecord(
        task=TASK,
        key=record_key(settings.model, settings.thinking, settings.max_tokens),
        model=settings.model,
        effort=settings.effort,
        thinking=settings.thinking,
        max_tokens=settings.max_tokens,
        suite=SUITE,
        runs=runs,
        passed=all(check.passed for check in found),
        checks=found,
        cost_usd=(sum((r.cost_usd for r in results), Decimal(0)) + extra_cost).quantize(Decimal("0.000001")),
        recorded_at=datetime.now(UTC),
    )


def misses(results: Sequence[Run]) -> Counter[str]:
    """Missed expectations by case, for the console report."""
    return Counter(f"{r.case}: {m}" for r in results for m in r.missed)


__all__ = ["CASES", "Cohort", "Run", "Taste", "checks", "judge", "misses", "record", "run_suite"]
