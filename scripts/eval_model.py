"""Model conformance suite: can this model run the FAST scheduling agent? (SPEC §6, models-ui.md §2).

Runs the standard and hard scheduling scenarios through the real agent and scores every run. A model should pass
every run before it goes into ``ALLOWED_MODELS`` or the ModelSpec parameter; the exit status is 0 only then.

Examples:
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
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from fast_channels.agent.evals.runner import Target, rescore, run_suite, save, summarise
from fast_channels.agent.evals.scenarios import SCENARIOS
from fast_channels.agent.model import Effort, ModelSpec


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
        specs = [
            ModelSpec(model_id=model_id, effort=Effort(chat=args.effort, plan=args.effort), max_tokens=args.max_tokens)
            for model_id in args.model_id
        ]
        scenarios = [SCENARIOS[name] for name in args.scenarios.split(",")]
        records = asyncio.run(
            run_suite(specs, scenarios, args.runs, target=target, region=args.region, workers=args.workers)
        )
        directory = save(records, args.out / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}")
        print(f"results: {directory}", file=sys.stderr)
    print(summarise(records))
    return 0 if all(record.result["pass"] for record in records) else 1


if __name__ == "__main__":
    sys.exit(main())
