"""cdk-nag v3 (AWS Solutions + Serverless packs) and CDK's CloudFormation rules run on every synth."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import aws_sqs as sqs

from fast_channels.core.demand import NETWORK_CONTEXT

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
        {"removal": "retain", "observability.transaction_search": "enable", "account-baseline": True, **GLOBAL_MODELS},
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


@pytest.mark.parametrize(
    ("simulator", "ad_server"),
    [("lambda", "none"), ("guidance_stack", "none"), ("lambda", "mock")],
    ids=["lambda", "guidance_stack", "mock-ad-server"],
)
def test_no_unacknowledged_findings_beside_the_guidance(
    fresh_deployment: Callable[..., Any], tmp_path: Path, simulator: str, ad_server: str
) -> None:
    """The ``fastm`` stage with the Demand stack, which exists once the guidance is deployed, and with the mock ad
    server (M8-09)."""
    network = {
        "vpc_id": "vpc-0123456789abcdef0",
        "private_subnets": [
            {"id": "subnet-0aaaaaaaaaaaaaaa1", "az": "us-east-1a", "route_table": "rtb-0aaaaaaaaaaaaaaa1"},
            {"id": "subnet-0bbbbbbbbbbbbbbb2", "az": "us-east-1b", "route_table": "rtb-0bbbbbbbbbbbbbbb2"},
        ],
        "task_security_group_id": "sg-0123456789abcdef0",
        "cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/fastm-pbs-PrebidCluster",
        "service_arn": "arn:aws:ecs:us-east-1:123456789012:service/fastm-pbs-PrebidCluster/fastm-pbs-Service",
        "cloudfront_domain": "d111111abcdef8.cloudfront.net",
        "target_group_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastm-pbs/0123456789abcd",
        "simulator_code_key": "simulator/0123456789abcdef.zip",
        "internal_alb_dns": "internal-fastm-pbs-Internal-1234567890.us-east-1.elb.amazonaws.com",
        "internal_alb_zone_id": "Z35SXDOTRQ7X7K",
    }
    deployment = fresh_deployment(
        {
            **FASTM,
            "modules.demand.simulator": simulator,
            "modules.demand.ad_server": ad_server,
            "modules.demand.dayparts": [{"cron": "cron(50 17 * * ? *)", "min": 3}],
        },
        outdir=tmp_path,
        extra={NETWORK_CONTEXT: network},
    )
    assert deployment.stage.demand is not None

    deployment.app.synth()

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []


def test_nag_packs_run_on_the_stage(fresh_deployment: Callable[..., Any], tmp_path: Path) -> None:
    deployment = fresh_deployment(outdir=tmp_path)
    sqs.CfnQueue(deployment.stage.media, "Unencrypted")

    with pytest.raises(RuntimeError, match="AwsSolutions-SQS4"):
        deployment.app.synth()
