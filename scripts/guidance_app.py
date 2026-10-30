"""The guidance's own CDK app with the Pod Decision Service's sidecar in its Prebid Server task (ADR-0036).

    cd build/prebid-guidance/checkout/source/infrastructure
    <the guidance's venv>/bin/python <repository>/scripts/guidance_app.py      # as cdk deploy --app runs it

``scripts/_demand.py`` deploys the guidance with this app when ``modules.pds`` is on, and with the guidance's
``app.py`` otherwise. It patches nothing: it imports the checkout's ``app`` module, replaces the stack class that
``build_app`` instantiates with a subclass of the guidance's own ``PrebidServerStack``, and calls ``build_app``
unchanged. The subclass builds the guidance's stack, then adds to it with escape hatches, the CDK way of augmenting a
construct one does not own (:func:`augment`):

- to each task definition of the guidance (``PrebidTaskDef``: one per entry deployment, of which CloudFormation
  conditions deploy one), the sidecar's container, which Prebid Server's container waits for (``HEALTHY``);
- the task's CPU and memory (``Cpu`` and ``Memory`` overrides of the L1 task definition);
- the sidecar's grants on the shared task role;
- its S3 reads in the policy of the guidance VPC's S3 gateway endpoint, which lets through only the guidance's own
  buckets otherwise;
- a DynamoDB gateway endpoint in the guidance's VPC, for the sidecar's tables only (the guidance calls no DynamoDB),
  so reservations do not go out through a NAT gateway;
- the sidecar's log group, at the stack's root (so no condition applies to it).

The sidecar comes as the CDK context ``fastPds`` (``core.pds.sidecar.Sidecar`` as JSON): its image, environment,
grants and sizes. This file runs in the guidance's Python (3.11, its pinned aws-cdk-lib) without this repository's
packages, so it reads that JSON as plain data. ``vendor/prebid-guidance/PIN.toml [relied_on]`` hashes every upstream
file it relies on: an upstream change fails ``prebid_guidance.py verify`` before it can reach a deploy.
"""

import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from aws_cdk import Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs

CONTEXT: Final = "fastPds"
"""``core.pds.sidecar.CONTEXT``."""
TASK_DEFINITION: Final = "PrebidTaskDef"
"""The guidance's task definitions' construct id (``prebid_server/ecs_task_construct.py``)."""
PREBID_CONTAINER: Final = "Prebid-Container"
"""The guidance's Prebid Server container."""
LOG_BUFFER: Final = Size.mebibytes(25)
"""The log driver's buffer in non-blocking mode (Docker's default is 1 MiB): a CloudWatch Logs slowdown is absorbed
before a line is dropped, and never blocks a request (the guidance logs Prebid Server the same way)."""
RESTART_AFTER: Final = Duration.seconds(60)
"""ECS restarts a sidecar that exits, in place, once it ran this long (a crash loop is not restarted)."""


class SidecarError(Exception):
    """The guidance's stack is not what this app relies on."""


def augment(stack: Stack, sidecar: Mapping[str, Any]) -> list[ecs.FargateTaskDefinition]:
    """Add ``sidecar`` (``core.pds.sidecar.Sidecar`` as data) to every Prebid Server task definition of ``stack``;
    returns them.

    Raises:
        SidecarError: no task definition, one with another id, one without Prebid Server's container, not exactly one
            S3 gateway endpoint, or not exactly one VPC.
    """
    nodes = stack.node.find_all()
    tasks = [node for node in nodes if isinstance(node, ecs.FargateTaskDefinition)]
    if not tasks or any(task.node.id != TASK_DEFINITION for task in tasks):
        found = [task.node.path for task in tasks]
        raise SidecarError(f"expected the guidance's {TASK_DEFINITION} task definitions, found {found}")
    endpoints = [
        node
        for node in nodes
        if isinstance(node, ec2.GatewayVpcEndpoint)
        and ".s3" in json.dumps(stack.resolve(node.node.default_child.service_name))  # type: ignore[union-attr]
    ]
    if len(endpoints) != 1:
        raise SidecarError(f"expected the guidance VPC's one S3 gateway endpoint, found {len(endpoints)}")
    for statement in sidecar["endpoint_statements"]:
        endpoints[0].add_to_policy(iam.PolicyStatement.from_json(statement))
    vpcs = [node for node in nodes if isinstance(node, ec2.Vpc)]
    if len(vpcs) != 1:
        raise SidecarError(f"expected the guidance's one VPC, found {[vpc.node.path for vpc in vpcs]}")
    dynamodb = vpcs[0].add_gateway_endpoint("PdsDynamoDbEndpoint", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB)
    for statement in sidecar["dynamodb_endpoint_statements"]:
        dynamodb.add_to_policy(iam.PolicyStatement.from_json(statement))
    log_group = logs.LogGroup(
        stack,
        "PdsSidecarLogs",
        log_group_name=sidecar["log_group"],
        retention=logs.RetentionDays.INFINITE,  # the days below: jsii's enum has no member by number
        removal_policy=RemovalPolicy.DESTROY,
    )
    retention = log_group.node.default_child
    assert isinstance(retention, logs.CfnLogGroup)
    retention.add_property_override("RetentionInDays", int(sidecar["log_retention_days"]))
    for task in tasks:
        prebid = task.find_container(PREBID_CONTAINER)
        if prebid is None:
            raise SidecarError(f"{task.node.path} has no {PREBID_CONTAINER}")
        pds = task.add_container(
            sidecar["container"],
            image=ecs.ContainerImage.from_registry(sidecar["image"]),
            essential=True,
            environment=dict(sidecar["environment"]),
            logging=ecs.LogDriver.aws_logs(
                stream_prefix="pds",
                log_group=log_group,
                mode=ecs.AwsLogDriverMode.NON_BLOCKING,
                max_buffer_size=LOG_BUFFER,
            ),
            health_check=ecs.HealthCheck(
                command=list(sidecar["health_command"]),
                interval=Duration.seconds(5),
                timeout=Duration.seconds(2),
                retries=3,
                start_period=Duration.seconds(10),
            ),
            readonly_root_filesystem=True,
            enable_restart_policy=True,
            restart_attempt_period=RESTART_AFTER,
        )
        prebid.add_container_dependencies(
            ecs.ContainerDependency(container=pds, condition=ecs.ContainerDependencyCondition.HEALTHY)
        )
        definition = task.node.default_child
        assert isinstance(definition, ecs.CfnTaskDefinition)
        definition.add_property_override("Cpu", str(sidecar["task_cpu"]))
        definition.add_property_override("Memory", str(sidecar["task_memory_mib"]))
        for statement in sidecar["statements"]:
            task.add_to_task_role_policy(iam.PolicyStatement.from_json(statement))
    return tasks


def main() -> None:
    """Build the guidance's app from the checkout in the working directory, with the sidecar."""
    sys.path.insert(0, str(Path.cwd()))  # the checkout's source/infrastructure: its app module and packages
    guidance: Any = importlib.import_module("app")
    shipped: Any = guidance.PrebidServerStack

    class PrebidServerWithPds(shipped):  # type: ignore[misc,valid-type]
        """The guidance's stack, then the sidecar."""

        def __init__(self, scope: Any, construct_id: str, **kwargs: Any) -> None:
            super().__init__(scope, construct_id, **kwargs)
            raw = self.node.try_get_context(CONTEXT)
            if not raw:
                raise SidecarError(f"no {CONTEXT} context: deploy with the guidance's app.py instead")
            augment(self, json.loads(raw) if isinstance(raw, str) else raw)

    guidance.PrebidServerStack = PrebidServerWithPds
    guidance.build_app()


if __name__ == "__main__":
    main()
