"""cdk-nag v3 (AWS Solutions + Serverless packs) and CDK's CloudFormation rules run on every synth."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import aws_sqs as sqs

GLOBAL_MODELS = {
    "agent.model": "global.anthropic.claude-haiku-5-5",
    "agent.allowed_models": ["global.anthropic.claude-haiku-5-5"],
}
FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
"""The ``fastm`` stage: every module on (its top-level keys deep-merge into cdk.json's block, as with ``STAGE=``)."""
TOPOLOGIES = {
    "default": ({}, "us-east-1"),
    "stage-fastm": (FASTM, "us-east-1"),
    "production-everything": (
        {"removal": "retain", "observability.transaction_search": "enable", **GLOBAL_MODELS},
        "eu-west-1",
    ),
    "media-only": ({"agent.enabled": False}, "eu-central-1"),
    "models-with-budgets-elsewhere": (
        {
            **GLOBAL_MODELS,
            "modules.models.enabled": True,
            "modules.models.budgets_action": True,
            "modules.models.tasks": {"scheduler.plan": {"model": "global.anthropic.claude-haiku-5-5"}},
            "notifications.email": "ops@example.com",
        },
        "eu-west-1",
    ),
    "as-run-router": (
        {"as-run-router": True, "observability.as_run": "shared", "modules.ops.enabled": True},
        "us-east-1",
    ),
    "external-ads-custom-domain-two-ladders": (
        {
            "ladders.builtin": ["standard", "mobile"],
            "ads": {"mode": "external_vast", "url": "https://ads.example.com/vast?sid=[session.uuid]"},
            "delivery.content_domain": "media.example.com",
            "delivery.certificate_arn": "arn:aws:acm:us-east-1:123456789012:certificate/0f1e2d3c",
            "channels.iac": [
                {"name": "fast-demo", "outputs": ["hls"]},
                {"name": "fast-mobile", "ladder": "mobile", "autopilot_cron": "rate(6 hours)"},
            ],
        },
        "us-east-1",
    ),
}


@pytest.mark.usefixtures("second_ladder")
@pytest.mark.parametrize(("overrides", "region"), TOPOLOGIES.values(), ids=TOPOLOGIES.keys())
def test_no_unacknowledged_findings(
    fresh_deployment: Callable[..., Any], tmp_path: Path, overrides: dict[str, Any], region: str
) -> None:
    deployment = fresh_deployment(overrides, region=region, outdir=tmp_path)

    deployment.app.synth()  # raises ValidationFailed listing every unacknowledged finding

    # cdk-nag v3 reports only plugins with findings; a clean synth writes an empty list.
    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []


def test_nag_packs_run_on_the_stage(fresh_deployment: Callable[..., Any], tmp_path: Path) -> None:
    deployment = fresh_deployment(outdir=tmp_path)
    sqs.CfnQueue(deployment.stage.media, "Unencrypted")

    with pytest.raises(RuntimeError, match="AwsSolutions-SQS4"):
        deployment.app.synth()
