"""The Release Controller's prefetcher (M3-06) in the Release stack: its settings, triggers, alarm and grants, the
reaper's grants on prefetch schedules, and nothing of it with ``modules.release.prefetch.enabled`` false."""

import json
from collections.abc import Callable
from typing import Any

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.monetization.prefetch import PrefetchPolicy

type GetDeployment = Callable[..., Any]

NETWORK: dict[str, Any] = {
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
}
EXTRA = {NETWORK_CONTEXT: NETWORK}
RELEASE = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}
BASE = "arn:aws:mediatailor:us-east-1:123456789012"
HANDLER = "fast_channels.functions.release.prefetch.handler"


def release(deployment: GetDeployment, **overrides: Any) -> dict[str, Any]:
    d = deployment(RELEASE | overrides, extra=EXTRA)
    resources: dict[str, Any] = d.template(d.stage.release).to_json()["Resources"]
    return resources


def of_type(resources: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {k: r["Properties"] for k, r in resources.items() if r["Type"] == kind}


def prefetcher(resources: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    [(name, function)] = [
        (k, p) for k, p in of_type(resources, "AWS::Lambda::Function").items() if p.get("Handler") == HANDLER
    ]
    return name, function


def statements_of(resources: dict[str, Any], role_prefix: str) -> list[dict[str, Any]]:
    return [
        s
        for p in of_type(resources, "AWS::IAM::Policy").values()
        if any(r["Ref"].startswith(role_prefix) for r in p.get("Roles", []))
        for s in p["PolicyDocument"]["Statement"]
    ]


def test_the_prefetcher_reads_the_stages_settings(deployment: GetDeployment) -> None:
    _, function = prefetcher(release(deployment))
    variables = function["Environment"]["Variables"]
    assert (variables["RELEASE_CHANNELS"], variables["PREFETCH_SIBLINGS"]) == ("fastm-demo", "a,b,k,l")
    assert variables["BREAK_LENGTHS_MS"] == "30030,60060,90090,120120"
    assert PrefetchPolicy.model_validate_json(variables["PREFETCH_POLICY"]) == PrefetchPolicy()
    assert next(iter(variables["RELEASE_TABLE"].values())).startswith("ControllerState")


def test_the_policy_follows_the_configuration(deployment: GetDeployment) -> None:
    resources = release(
        deployment,
        **{
            "modules.release.prefetch.horizon_breaks": 12,
            "modules.release.prefetch.shaping_window_seconds": 0,
            "modules.release.prefetch.siblings": ["b"],
        },
    )
    variables = prefetcher(resources)[1]["Environment"]["Variables"]
    policy = PrefetchPolicy.model_validate_json(variables["PREFETCH_POLICY"])
    assert (policy.horizon_breaks, policy.shaping_window_s, variables["PREFETCH_SIBLINGS"]) == (12, 0, "b")


def test_it_runs_every_five_minutes_and_after_a_re_plan_or_a_channel_start(deployment: GetDeployment) -> None:
    resources = release(deployment)
    name, _ = prefetcher(resources)
    [schedule] = [s for s in of_type(resources, "AWS::Scheduler::Schedule").values() if "prefetch" in s["Description"]]
    assert schedule["ScheduleExpression"] == "rate(5 minutes)"
    [rule] = [r for k, r in of_type(resources, "AWS::Events::Rule").items() if k.startswith("Prefetcher")]
    assert rule["EventPattern"] == {
        "detail-type": ["Schedule Published", "Census Changed"],
        "source": ["fast-channels.fastm"],
    }
    assert rule["Targets"][0]["Arn"] == {"Fn::GetAtt": [name, "Arn"]}
    [alarm] = [a for a in of_type(resources, "AWS::CloudWatch::Alarm").values() if "prefetch" in a["AlarmName"]]
    assert alarm["AlarmName"] == "fastm-release-prefetch" and alarm["Threshold"] == 1


def test_it_may_write_only_the_siblings_prefetch_schedules(deployment: GetDeployment) -> None:
    resources = release(deployment)
    granted = {
        (action, json.dumps(s["Resource"]))
        for s in statements_of(resources, "PrefetcherFunctionRole")
        for action in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
        if action.startswith("mediatailor:")
    }
    assert granted == {
        ("mediatailor:DescribeChannel", json.dumps(f"{BASE}:channel/fastm-demo")),
        ("mediatailor:GetChannelSchedule", json.dumps(f"{BASE}:channel/fastm-demo")),
        ("mediatailor:DescribeProgram", json.dumps(f"{BASE}:program/fastm-demo/*")),
        ("mediatailor:ListPrefetchSchedules", json.dumps(f"{BASE}:prefetchSchedule/*")),
        ("mediatailor:CreatePrefetchSchedule", json.dumps(f"{BASE}:prefetchSchedule/fastm-demo-p-*/*")),
        ("mediatailor:DeletePrefetchSchedule", json.dumps(f"{BASE}:prefetchSchedule/fastm-demo-p-*/*")),
        ("mediatailor:TagResource", json.dumps(f"{BASE}:prefetchSchedule/fastm-demo-p-*/*")),
    }


def test_the_reaper_deletes_the_siblings_schedules(deployment: GetDeployment) -> None:
    reaper = statements_of(release(deployment), "ControllerReaperRole")
    actions = {a for s in reaper for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert {"mediatailor:ListPrefetchSchedules", "mediatailor:DeletePrefetchSchedule"} <= actions


def test_off_there_is_no_prefetcher(deployment: GetDeployment) -> None:
    resources = release(deployment, **{"modules.release.prefetch.enabled": False})
    assert not [p for p in of_type(resources, "AWS::Lambda::Function").values() if p.get("Handler") == HANDLER]
