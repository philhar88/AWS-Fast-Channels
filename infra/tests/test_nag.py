"""cdk-nag v3 (AWS Solutions + Serverless packs) and CDK's CloudFormation rules run on every synth."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

TOPOLOGIES = {
    "default": ({}, "us-east-1"),
    "production-everything": (
        {
            "removal": "retain",
            "observability.transaction_search": "enable",
            "agent.model": "global.anthropic.claude-haiku-5-5",
            "agent.allowed_models": ["global.anthropic.claude-haiku-5-5"],
        },
        "eu-west-1",
    ),
    "media-only": ({"agent.enabled": False}, "eu-central-1"),
}


@pytest.mark.parametrize(("overrides", "region"), TOPOLOGIES.values(), ids=TOPOLOGIES.keys())
def test_no_unacknowledged_findings(
    fresh_deployment: Callable[..., Any], tmp_path: Path, overrides: dict[str, Any], region: str
) -> None:
    deployment = fresh_deployment(overrides, region=region, outdir=tmp_path)

    deployment.app.synth()  # raises ValidationFailed listing every unacknowledged finding

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert {r["conclusion"] for r in reports} == {"success"}
    assert [v for r in reports for v in r.get("violations", [])] == []
