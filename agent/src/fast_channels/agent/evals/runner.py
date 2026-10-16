"""Run conformance scenarios through the real agent and summarise them (``scripts/eval_model.py``).

Each run is a fresh conversation: the scenario prompt, then automatic approval of every ``approval_required``
(the operator always says yes), until the agent finishes. Transcripts are kept so runs can be re-scored offline.
"""

import asyncio
import json
import statistics
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from ag_ui.core import RunAgentInput
from strands.session import FileSessionManager

from fast_channels.agent.evals.fake_tools import CHANNEL, FakeScheduleTools, serve
from fast_channels.agent.evals.scenarios import HOUR_MS, SCENARIOS, Scenario
from fast_channels.agent.evals.scoring import score
from fast_channels.agent.model import ModelSpec, bedrock_model
from fast_channels.agent.protocol import APPROVAL_EVENT
from fast_channels.agent.scheduler import Scheduler
from fast_channels.agent.settings import Settings
from fast_channels.agent.testing import OPERATOR_CLAIMS, unsigned_jwt

CONFIRM_TOOLS: Final = frozenset({"schedule___publish"})
MAX_RUNS_PER_CONVERSATION: Final = 6

# USD per 1M tokens: input, output, cache read, cache write (5 min); AWS Price List API, us-east-1, 2026-10-15
# (models-ui.md §1.2). Models not listed are reported without cost.
PRICES: Final[dict[str, tuple[float, float, float, float]]] = {
    "global.anthropic.claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "us.anthropic.claude-haiku-5-5": (0.11, 0.55, 0.011, 0.1375),
    "global.anthropic.claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "global.amazon.nova-2-lite-v1:0": (0.30, 2.50, 0.075, 0.0),
    "us.amazon.nova-2-lite-v1:0": (0.33, 2.75, 0.0825, 0.0),
}


@dataclass(frozen=True, slots=True)
class Target:
    """Where the tools come from: the local fake server (default) or a live Gateway with a real token."""

    gateway_url: str | None = None
    token: str | None = None
    channel: str = CHANNEL


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One scored conversation."""

    scenario: str
    model_id: str
    effort: str
    run: int
    start_ms: int
    latency_s: float
    agent_runs: int
    usage: dict[str, int]
    cost_usd: float | None
    error: str | None
    result: dict[str, Any]
    transcript: list[dict[str, Any]]


async def run_scenario(spec: ModelSpec, scenario: Scenario, index: int, *, target: Target, region: str) -> RunRecord:
    """Run one conversation for ``scenario`` on ``spec`` and score it."""
    start_ms = _next_block_start()
    transcript: list[dict[str, Any]] = []
    began = time.perf_counter()
    with ExitStack() as stack:
        url = target.gateway_url or stack.enter_context(serve(FakeScheduleTools(channel=target.channel)))
        sessions_dir = stack.enter_context(tempfile.TemporaryDirectory())
        scheduler = Scheduler(
            Settings(region=region, gateway_url=url, confirm_tools=CONFIRM_TOOLS),
            model=lambda _spec, mode: bedrock_model(spec, mode, region),
            sessions=lambda session_id, _caller: FileSessionManager(session_id=session_id, storage_dir=sessions_dir),
        )
        headers = {"Authorization": f"Bearer {target.token or unsigned_jwt(**OPERATOR_CLAIMS)}"}
        session_id = f"eval-{scenario.name}-{uuid.uuid4()}"
        message: str | None = scenario.prompt(start_ms, target.channel)
        props: dict[str, Any] = {"mode": "plan"}
        for _ in range(MAX_RUNS_PER_CONVERSATION):
            run_input = RunAgentInput(
                thread_id=session_id,
                run_id=str(uuid.uuid4()),
                forwarded_props=props,
                messages=[{"id": str(uuid.uuid4()), "role": "user", "content": message}] if message else [],  # type: ignore[list-item]
            )
            events = [
                e.model_dump(mode="json", by_alias=True)
                async for e in scheduler.run(run_input, session_id=session_id, headers=headers)
            ]
            transcript.extend(events)
            pending = [
                e["value"]["interrupt_id"] for e in events if e["type"] == "CUSTOM" and e.get("name") == APPROVAL_EVENT
            ]
            if not pending:
                break
            message, props = None, {"mode": "plan", "approval": {"interrupt_id": pending[0], "approved": True}}
    usage = _usage(transcript)
    error = next((f"{e.get('code')}: {e.get('message')}" for e in transcript if e["type"] == "RUN_ERROR"), None)
    return RunRecord(
        scenario=scenario.name,
        model_id=spec.model_id,
        effort=spec.effort.plan,
        run=index,
        start_ms=start_ms,
        latency_s=round(time.perf_counter() - began, 2),
        agent_runs=sum(e["type"] == "RUN_STARTED" for e in transcript),
        usage=usage,
        cost_usd=_cost(spec.model_id, usage),
        error=error,
        result=score(transcript, scenario, start_ms),
        transcript=transcript,
    )


async def run_suite(
    specs: Sequence[ModelSpec],
    scenarios: Sequence[Scenario],
    runs: int,
    *,
    target: Target,
    region: str,
    workers: int = 4,
) -> list[RunRecord]:
    """Every (model, scenario, run) combination, ``workers`` conversations at a time."""
    gate = asyncio.Semaphore(workers)

    async def one(spec: ModelSpec, scenario: Scenario, index: int) -> RunRecord:
        async with gate:
            return await run_scenario(spec, scenario, index, target=target, region=region)

    return list(
        await asyncio.gather(
            *(one(spec, scenario, index) for spec in specs for scenario in scenarios for index in range(1, runs + 1))
        )
    )


def save(records: Iterable[RunRecord], directory: Path) -> Path:
    """Write ``runs.jsonl`` (with transcripts) and ``summary.md``; returns the directory."""
    records = list(records)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "runs.jsonl").open("w") as sink:
        for record in records:
            sink.write(json.dumps(asdict(record)) + "\n")
    (directory / "summary.md").write_text(summarise(records) + "\n")
    return directory


def rescore(directories: Iterable[Path]) -> list[RunRecord]:
    """Re-score saved transcripts with the current checks (no model calls)."""
    records = []
    for directory in directories:
        for line in (directory / "runs.jsonl").read_text().splitlines():
            record = RunRecord(**json.loads(line))
            result = score(record.transcript, SCENARIOS[record.scenario], record.start_ms)
            records.append(RunRecord(**{**asdict(record), "result": result}))
    return records


def summarise(records: Sequence[RunRecord]) -> str:
    """A markdown table per (model, effort, scenario), like the benchmark summaries."""
    groups: dict[tuple[str, str, str], list[RunRecord]] = defaultdict(list)
    for record in records:
        groups[(record.model_id, record.effort, record.scenario)].append(record)
    rows = [
        "| model | effort | scenario | n | pass | mean score | failed checks | tool calls | in/out tok | "
        "latency s (median) | cost $ (mean) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (model_id, effort, scenario), group in groups.items():
        failed: dict[str, int] = defaultdict(int)
        for record in group:
            for check, ok in record.result["checks"].items():
                failed[check] += not ok
        failures = ", ".join(f"{check}({n})" for check, n in failed.items() if n) or "-"
        costs = [r.cost_usd for r in group if r.cost_usd is not None]
        rows.append(
            f"| {model_id} | {effort} | {scenario} | {len(group)} "
            f"| {sum(r.result['pass'] for r in group)}/{len(group)} "
            f"| {statistics.mean(r.result['score'] for r in group):.2f} | {failures} "
            f"| {statistics.mean(r.result['tool_calls'] for r in group):.1f} "
            f"| {statistics.mean(r.usage['input'] for r in group):.0f}/"
            f"{statistics.mean(r.usage['output'] for r in group):.0f} "
            f"| {statistics.median(r.latency_s for r in group):.1f} "
            f"| {f'{statistics.mean(costs):.5f}' if costs else 'n/a'} |"
        )
    return "\n".join(rows)


def _usage(transcript: Iterable[dict[str, Any]]) -> dict[str, int]:
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for event in transcript:
        if event["type"] != "RUN_FINISHED":
            continue
        for entry in event.get("usage") or ():
            totals["input"] += entry.get("inputTokens", 0)
            totals["output"] += entry.get("outputTokens", 0)
            totals["cache_read"] += entry.get("cachedInputTokens", 0)
            totals["cache_write"] += entry.get("cacheWriteInputTokens", 0)
    return totals


def _cost(model_id: str, usage: dict[str, int]) -> float | None:
    if (prices := PRICES.get(model_id)) is None:
        return None
    p_in, p_out, p_read, p_write = prices
    uncached = usage["input"] - usage["cache_read"] - usage["cache_write"]  # AG-UI input includes cache tokens
    return round(
        (uncached * p_in + usage["output"] * p_out + usage["cache_read"] * p_read + usage["cache_write"] * p_write)
        / 1e6,
        6,
    )


def _next_block_start() -> int:
    """The next full UTC hour at least 30 minutes away (the plan tool warns under 10 minutes of lead time)."""
    soon = int(time.time() * 1000) + 30 * 60_000
    return -(-soon // HOUR_MS) * HOUR_MS
