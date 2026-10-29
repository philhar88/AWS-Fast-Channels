"""Model conformance suite: can this model run the FAST scheduling agent? (SPEC §6, models-ui.md §2).

Runs the standard and hard scheduling scenarios through the real agent and scores every run. A model should pass
every run before it goes into ``ALLOWED_MODELS`` or the ModelSpec parameter; the exit status is 0 only then.

With ``--task`` (``make eval TASK=scheduler.plan MODEL=<id>``) the suite runs with that registry task's settings from
cdk.json (effort, thinking, max_tokens; ``--effort`` and ``--max-tokens`` are ignored) and writes the eval record
``evals/records/<task>/<key>.json``, passing or not: commit a passing one and name its key in the task's ``eval``.
With the model registry on, synth refuses a task whose ``eval`` names no passing record for its model and settings.

Examples:
    uv run scripts/eval_model.py --task scheduler.plan --model-id us.anthropic.claude-haiku-5-5
    uv run scripts/eval_model.py --model-id us.anthropic.claude-haiku-5-5 --runs 3
    uv run scripts/eval_model.py --model-id us.anthropic.claude-sonnet-5-5 --effort low --scenarios hard
    uv run scripts/eval_model.py --model-id us.anthropic.claude-haiku-5-5 \\
        --gateway-url https://<id>.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \\
        --token-file ~/.fast/token --channel <prefix>-agent-eval
    uv run scripts/eval_model.py --rescore evals-results/20261016T120000Z

The default target is a local fake tool server (no AWS resources besides Bedrock). With ``--gateway-url`` the
tools are the deployed ones, called with the operator access token in ``--token-file``: use a sandbox channel
and a catalog that contains the eval titles.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from fast_channels.agent.evals.runner import RunRecord, Target, rescore, run_suite, save, summarise
from fast_channels.agent.evals.scenarios import SCENARIOS
from fast_channels.agent.model import Effort, ModelSpec
from fast_channels.core.model_registry import (
    SCHEDULER_TASK,
    EvalCheck,
    EvalRecord,
    ModeEffort,
    TaskSettings,
    record_key,
    record_path,
)

REPO: Final = Path(__file__).resolve().parents[1]
RECORDS: Final = REPO / "evals" / "records"
SUITES: Final = {SCHEDULER_TASK: "scheduler conformance v1 (fake Gateway tools): standard, hard"}
"""The tasks with a conformance suite here; the recommender's ships with its module (Phase 2)."""


def task_settings(task: str, model_id: str, cdk_json: Path = REPO / "cdk.json") -> TaskSettings:
    """``task``'s registry settings in cdk.json, with ``model_id`` as its model."""
    tasks = json.loads(cdk_json.read_text())["context"]["fast-channels"]["modules"]["models"]["tasks"]
    if task not in tasks:
        raise SystemExit(f"cdk.json has no model-registry task {task!r} (modules.models.tasks: {sorted(tasks)})")
    return TaskSettings.model_validate({**tasks[task], "model": model_id})


def task_spec(settings: TaskSettings) -> ModelSpec:
    """The agent's spec for ``settings``: one effort level applies to both modes."""
    effort = settings.effort
    chat, plan = (effort.chat, effort.plan) if isinstance(effort, ModeEffort) else (effort, effort)
    return ModelSpec(
        model_id=settings.model,
        effort=Effort(chat=chat.value, plan=plan.value),
        max_tokens=settings.max_tokens,
        thinking=settings.thinking.value,
    )


def task_record(task: str, settings: TaskSettings, runs: int, records: Sequence[RunRecord]) -> EvalRecord:
    """The eval record of a suite run: one check per scenario (every run passes)."""
    by_scenario: dict[str, list[RunRecord]] = {}
    for record in records:
        by_scenario.setdefault(record.scenario, []).append(record)
    checks = tuple(
        EvalCheck(
            name=f"scenario_{scenario}",
            passed=all(r.result["pass"] for r in group),
            value=sum(r.result["pass"] for r in group) / len(group),
            threshold=f"{len(group)}/{len(group)} runs",
        )
        for scenario, group in sorted(by_scenario.items())
    )
    return EvalRecord(
        task=task,
        key=record_key(settings.model, settings.thinking, settings.max_tokens),
        model=settings.model,
        effort=settings.effort,
        thinking=settings.thinking,
        max_tokens=settings.max_tokens,
        suite=SUITES[task],
        runs=runs,
        passed=all(check.passed for check in checks),
        checks=checks,
        cost_usd=sum((Decimal(str(r.cost_usd or 0)) for r in records), Decimal(0)).quantize(Decimal("0.000001")),
        recorded_at=datetime.now(UTC),
    )


def write_record(record: EvalRecord, directory: Path = RECORDS) -> Path:
    """Write ``record`` as ``<directory>/<task>/<key>.json``."""
    path = record_path(directory, record.task, record.key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model-id",
        action="append",
        default=[],
        help="Bedrock model or inference profile id (repeat to compare models)",
    )
    parser.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "max"],
        help="reasoning effort for the run (Anthropic models)",
    )
    parser.add_argument("--max-tokens", type=int, default=16_000)
    parser.add_argument("--task", choices=sorted(SUITES), help="registry task: its settings, and write its eval record")
    parser.add_argument("--records", type=Path, default=RECORDS, help="eval records directory (with --task)")
    parser.add_argument("--scenarios", default=",".join(SCENARIOS), help=f"comma-separated: {', '.join(SCENARIOS)}")
    parser.add_argument("--runs", type=int, default=3, help="runs per model and scenario")
    parser.add_argument("--workers", type=int, default=4, help="conversations in parallel")
    parser.add_argument("--gateway-url", help="live AgentCore Gateway MCP endpoint (default: local fake tools)")
    parser.add_argument("--token-file", type=Path, help="file holding a Cognito access token for --gateway-url")
    parser.add_argument("--channel", default=None, help="channel to schedule (default: the fake tools' channel)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--out", type=Path, default=Path("evals-results"), help="results directory")
    parser.add_argument("--rescore", nargs="+", type=Path, metavar="DIR", help="re-score saved runs and exit")
    args = parser.parse_args()

    if args.rescore:
        records = rescore(args.rescore)
    else:
        if not args.model_id:
            parser.error("--model-id is required")
        if args.gateway_url and not args.token_file:
            parser.error("--gateway-url needs --token-file")
        target = Target(
            gateway_url=args.gateway_url,
            token=args.token_file.expanduser().read_text().strip() if args.token_file else None,
            **({"channel": args.channel} if args.channel else {}),
        )
        if args.task and len(args.model_id) != 1:
            parser.error("--task evaluates one --model-id")
        settings = task_settings(args.task, args.model_id[0]) if args.task else None
        specs = (
            [task_spec(settings)]
            if settings
            else [
                ModelSpec(
                    model_id=model_id, effort=Effort(chat=args.effort, plan=args.effort), max_tokens=args.max_tokens
                )
                for model_id in args.model_id
            ]
        )
        scenarios = [SCENARIOS[name] for name in args.scenarios.split(",")]
        records = asyncio.run(
            run_suite(specs, scenarios, args.runs, target=target, region=args.region, workers=args.workers)
        )
        directory = save(records, args.out / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        print(f"results: {directory}", file=sys.stderr)
        if settings:
            record = task_record(args.task, settings, args.runs, records)
            print(f"eval record ({'passed' if record.passed else 'FAILED'}): {write_record(record, args.records)}")
    print(summarise(records))
    return 0 if all(record.result["pass"] for record in records) else 1


if __name__ == "__main__":
    sys.exit(main())
