"""The census gate, the daypart scheduled actions and the warm-up of new Prebid Server tasks (M8-05)."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import (
    NETWORK_CONTEXT,
    SCALE_IN_AFTER_SECONDS,
    VEGETA,
    WARMUP_HEALTHY_WAIT_SECONDS,
    WARMUP_RATE_PER_TASK,
)
from fast_channels.infra.config import DemandTasks
from fast_channels.infra.constructs.demand.scaling import census_gate_definition
from fast_channels.infra.constructs.demand.warmup import warmup_buildspec

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

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
DAYPARTS = [{"cron": "cron(50 17 * * ? *)", "min": 3}, {"cron": "cron(0 23 * * ? *)", "min": 2}]
DEMAND = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.demand.dayparts": DAYPARTS,
}
TASKS = DemandTasks(min=2, max=4)


def resources(template: Template, kind: str) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template.find_resources(kind).values()]


def run(definition: dict[str, Any], running: int, parameter: str = "0") -> list[str]:
    """The states a gate execution passes through (a tiny JSONata-free walk of the definition's choices)."""
    states, path, name = definition["States"], [], definition["StartAt"]
    while True:
        path.append(name)
        state = states[name]
        match state["Type"]:
            case "Choice":
                condition = state["Choices"][0]["Condition"]
                taken = (running > 0) if "running > 0" in condition else (int(parameter) == 0)
                name = state["Choices"][0]["Next"] if taken else state["Default"]
            case "Succeed":
                return path
            case _ if state.get("End"):
                return path
            case _:
                name = state["Next"]


def test_the_gate_raises_the_tier_minimum_when_a_channel_starts() -> None:
    definition = census_gate_definition(cluster="c", service="s", parameter="/fastm/ops/running-channels", tasks=TASKS)

    assert run(definition, running=1) == ["AnyChannel", "CurrentMinimum", "RaiseMinimum", "CurrentCount", "ScaleOut"]
    current = definition["States"]["CurrentMinimum"]
    assert current["Assign"]["floor"] == "{% $max([$states.result.ScalableTargets[0].MinCapacity, 2]) %}", (
        "a daypart's higher minimum stays"
    )
    raise_min = definition["States"]["RaiseMinimum"]["Arguments"]
    assert (raise_min["ResourceId"], raise_min["MinCapacity"], raise_min["MaxCapacity"]) == (
        "service/c/s",
        "{% $floor %}",
        4,
    )
    assert definition["States"]["ScaleOut"]["Arguments"]["DesiredCount"] == "{% $max([$desired, $floor]) %}"


def test_the_gate_scales_to_zero_only_after_15_minutes_without_a_channel() -> None:
    definition = census_gate_definition(cluster="c", service="s", parameter="/fastm/ops/running-channels", tasks=TASKS)

    assert run(definition, running=0, parameter="0") == [
        "AnyChannel",
        "WaitBeforeScaleIn",
        "StillNoChannel",
        "NoneRunning",
        "LowerMinimum",
        "ScaleIn",
    ]
    assert run(definition, running=0, parameter="1")[-1] == "ChannelsBack", "a channel restarted within 15 minutes"
    assert definition["States"]["WaitBeforeScaleIn"]["Seconds"] == SCALE_IN_AFTER_SECONDS == 900
    assert definition["States"]["ScaleIn"]["Arguments"]["DesiredCount"] == 0
    assert definition["States"]["LowerMinimum"]["Arguments"]["MinCapacity"] == 0


def test_the_warmup_replays_probe_auctions_with_a_verified_vegeta() -> None:
    spec = warmup_buildspec(
        prefix="fastm",
        cloudfront_domain="d1.cloudfront.net",
        cluster_arn=NETWORK["cluster_arn"],
        target_group_arn=NETWORK["target_group_arn"],
    )
    install, build = spec["phases"]["install"]["commands"], " ".join(spec["phases"]["build"]["commands"])
    pre_build = " ".join(spec["phases"]["pre_build"]["commands"])

    assert VEGETA["url"] in install[0] and f'echo "{VEGETA["sha256"]}  vegeta.tar.gz" | sha256sum -c -' in install
    assert "POST https://d1.cloudfront.net/openrtb2/auction?" in build
    assert "X-Fast-Probe: 1" in build, "probes: own book and house only"
    assert "X-Fast-T" not in build, "no ticket"
    assert "--secret-id fastm/demand/origin-a" in build
    assert "-rate=${RATE}/s" in build
    assert f"RATE=${{RATE:-$(( {WARMUP_RATE_PER_TASK} * (HEALTHY > 0 ? HEALTHY : 1) ))}}" in pre_build
    assert "fast-pod-120s" in build
    # Load only once the task is a healthy target: before, it fails its checks and ECS replaces it
    assert 'if [ -n "${TASK_ARN:-}" ]' in pre_build
    assert f"describe-target-health --target-group-arn {NETWORK['target_group_arn']}" in pre_build
    assert "TargetHealthDescriptions[?Target.Id=='$IP'].TargetHealth.State | [0]" in pre_build, "any port"
    assert '[ "$STATE" = healthy ] && break' in pre_build
    assert f"seq 1 {WARMUP_HEALTHY_WAIT_SECONDS // 10}" in pre_build


def test_the_demand_stack_scales_and_warms_the_guidances_service(deployment: GetDeployment) -> None:
    d = deployment(DEMAND, extra={NETWORK_CONTEXT: NETWORK})
    template = d.template(d.stage.demand)
    [machine] = resources(template, "AWS::StepFunctions::StateMachine")
    rules = resources(template, "AWS::Events::Rule")
    [project] = resources(template, "AWS::CodeBuild::Project")
    dayparts = resources(template, "Custom::AWS")

    assert machine["StateMachineName"] == "fastm-demand-census-gate"
    [census] = [r for r in rules if r["EventPattern"].get("detail-type") == ["Census Changed"]]
    assert census["EventBusName"] == "fastm-fast-channels"
    assert census["EventPattern"]["source"] == ["fast-channels.fastm"]
    [new_task] = [r for r in rules if r["EventPattern"].get("source") == ["aws.ecs"]]
    assert new_task["EventPattern"]["detail"]["group"] == ["service:fastm-pbs-Service"]
    assert new_task["EventPattern"]["detail"]["lastStatus"] == ["RUNNING"]
    assert all("DeadLetterConfig" in target for rule in (census, new_task) for target in rule["Targets"])
    [warm_target] = new_task["Targets"]
    transformer = warm_target["InputTransformer"]
    assert transformer["InputPathsMap"] == {"detail-taskArn": "$.detail.taskArn"}
    override = json.loads(transformer["InputTemplate"].replace("<detail-taskArn>", '"TASK"'))
    assert override == {"environmentVariablesOverride": [{"name": "TASK_ARN", "value": "TASK", "type": "PLAINTEXT"}]}
    assert project["Name"] == "fastm-pbs-warmup"
    assert project["Environment"]["Type"] == "ARM_CONTAINER"
    calls = [json.loads(r["Create"]) if isinstance(r["Create"], str) else r["Create"] for r in dayparts]
    assert len(calls) == len(DAYPARTS)
    assert "putScheduledAction" in json.dumps(calls)
    assert "cron(50 17 * * ? *)" in json.dumps(calls)


@pytest.mark.parametrize("dayparts", [[], DAYPARTS])
def test_dayparts_are_one_scheduled_action_each(deployment: GetDeployment, dayparts: list[dict[str, Any]]) -> None:
    d = deployment({**DEMAND, "modules.demand.dayparts": dayparts}, extra={NETWORK_CONTEXT: NETWORK})

    assert len(resources(d.template(d.stage.demand), "Custom::AWS")) == len(dayparts)
