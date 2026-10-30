"""Every invoke permission an AWS service gets is scoped to the resource that invokes, in every stack of the repo.

A permission for a service principal without ``SourceArn`` or ``SourceAccount`` lets that service invoke the
function on any account's behalf (the confused-deputy pattern): CDK's ``LambdaTarget`` grants Elastic Load Balancing
exactly that, so the repository's load balancers use ``ScopedLambdaTarget``.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
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
    "container_log_group": "fastm-pbs-PrebidContainerLogs",
}
TOPOLOGIES: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "default": ({}, {}),
    "every-module-beside-the-guidance": ({**FASTM, "modules.demand.simulator": "lambda"}, {NETWORK_CONTEXT: NETWORK}),
    "as-run-router": ({"as-run-router": True, "observability.as_run": "shared", "modules.ops.enabled": True}, {}),
}


@pytest.mark.parametrize(("overrides", "extra"), TOPOLOGIES.values(), ids=TOPOLOGIES.keys())
def test_every_service_principals_invoke_permission_names_its_source(
    deployment: Callable[..., Any], overrides: dict[str, Any], extra: dict[str, Any]
) -> None:
    d = deployment(overrides, extra=extra)
    unscoped = [
        f"{stack.stack_name}/{name} ({resource['Properties']['Principal']})"
        for stack in d.stacks
        for name, resource in d.template(stack).find_resources("AWS::Lambda::Permission").items()
        if str(resource["Properties"].get("Principal", "")).endswith(".amazonaws.com")
        and not {"SourceArn", "SourceAccount"} & set(resource["Properties"])
    ]
    assert not unscoped, unscoped
    if overrides:
        scoped = [
            stack
            for stack in d.stacks
            for resource in d.template(stack).find_resources("AWS::Lambda::Permission").values()
            if resource["Properties"].get("Principal") == "elasticloadbalancing.amazonaws.com"
        ]
        assert len(scoped) == (4 if extra else 0), "the PDS's three aliases and the simulator"
