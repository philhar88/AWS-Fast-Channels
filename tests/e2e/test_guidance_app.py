"""Offline tests of scripts/guidance_app.py (not marked e2e): the PDS sidecar added to the guidance's task.

``augment`` is checked on a stand-in of the guidance's stack with this repository's CDK. The test that synthesizes the
real guidance through the wrapper, in the guidance's own Python and CDK, runs where ``prebid_guidance.py apply`` has
filled ``build/prebid-guidance/checkout`` (the CI job ``guidance-overlays``); otherwise it is skipped.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from aws_cdk import App, CfnCondition, Fn, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk.assertions import Template
from constructs import Construct

from fast_channels.core.pds.sidecar import CONTEXT, Sidecar, sidecar

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"fast_script_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wrapper = _load("guidance_app")
guidance = _load("prebid_guidance")
demand = sys.modules["_demand"]
CHECKOUT = guidance.DEFAULT_CHECKOUT

SPEC: Sidecar = sidecar(
    prefix="fastcid",
    image="123456789012.dkr.ecr.us-east-1.amazonaws.com/fastcid-pds:0123456789abcdef",
    partition="aws",
    region="us-east-1",
    account="123456789012",
    config_bucket="fastcid-pds-config-0123",
    content_base_url="https://d111111abcdef8.cloudfront.net",
    log_retention_days=7,
)


def _vpc(stack: Stack) -> None:
    """The guidance's VPC: an S3 gateway endpoint whose policy allows only the guidance's own bucket."""
    vpc = ec2.Vpc(stack, "PrebidVpc", max_azs=2, nat_gateways=0)
    endpoint = vpc.add_gateway_endpoint("s3_gateway_endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)
    endpoint.add_to_policy(
        iam.PolicyStatement(
            principals=[iam.AnyPrincipal()], actions=["s3:GetObject"], resources=["arn:aws:s3:::guidance/*"]
        )
    )


class _Entry(Construct):
    """One of the guidance's entry deployments: a task definition ``ECSTask/PrebidTaskDef`` with Prebid Server's
    container, every resource under the deployment's condition."""

    def __init__(self, scope: Construct, id: str, condition: str) -> None:
        super().__init__(scope, id)
        task = ecs.FargateTaskDefinition(Construct(self, "ECSTask"), "PrebidTaskDef", cpu=2048, memory_limit_mib=4096)
        task.add_container("Prebid-Container", image=ecs.ContainerImage.from_registry("prebid"))
        vpc = ec2.Vpc.from_vpc_attributes(
            self, "Vpc", vpc_id="vpc-1", availability_zones=["us-east-1a"], private_subnet_ids=["subnet-1"]
        )
        elbv2.ApplicationTargetGroup(
            Construct(self, "ECSService"),
            "ALBTargetGroup",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            target_type=elbv2.TargetType.IP,
            vpc=vpc,
        )
        stack = Stack.of(self)
        cfn = stack.node.try_find_child(condition) or CfnCondition(
            stack, condition, expression=Fn.condition_equals("a", "a")
        )
        self.condition = cfn


def guidance_like(*, task_id: str = "PrebidTaskDef") -> Stack:
    stack = Stack(App(), "fastcid-pbs")
    _vpc(stack)
    _Entry(stack, "CloudFrontEntryDeployment", "CloudFront")
    _Entry(stack, "ALBEntryDeployment", "ALB")
    if task_id != "PrebidTaskDef":
        ecs.FargateTaskDefinition(stack, task_id, cpu=256, memory_limit_mib=512)
    return stack


def containers(template: Template) -> list[dict[str, Any]]:
    return [
        container
        for definition in template.find_resources("AWS::ECS::TaskDefinition").values()
        for container in definition["Properties"]["ContainerDefinitions"]
    ]


def test_every_task_definition_gets_the_sidecar_that_prebid_server_waits_for() -> None:
    stack = guidance_like()
    tasks = wrapper.augment(stack, json.loads(SPEC.model_dump_json()))
    template = Template.from_stack(stack)

    assert len(tasks) == 2
    definitions = template.find_resources("AWS::ECS::TaskDefinition").values()
    assert {(d["Properties"]["Cpu"], d["Properties"]["Memory"]) for d in definitions} == {("2048", "6144")}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for container in containers(template):
        by_name.setdefault(container["Name"], []).append(container)
    assert len(by_name["Pds-Container"]) == 2
    for prebid in by_name["Prebid-Container"]:
        assert prebid["DependsOn"] == [{"ContainerName": "Pds-Container", "Condition": "HEALTHY"}]
    pds = by_name["Pds-Container"][0]
    assert pds["Image"] == SPEC.image
    assert (pds["Essential"], pds["ReadonlyRootFilesystem"]) == (True, True)
    assert pds["RestartPolicy"] == {"Enabled": True, "RestartAttemptPeriod": 60}
    assert pds["HealthCheck"]["Command"] == list(SPEC.health_command)
    assert {"PortMappings", "Memory"}.isdisjoint(pds), "loopback only, the task's memory shared"
    assert (pds["Cpu"], by_name["Prebid-Container"][0]["Cpu"]) == (512, 1536), "CPU weights: a quarter for the sidecar"
    assert {e["Name"]: e["Value"] for e in pds["Environment"]} == SPEC.environment
    options = pds["LogConfiguration"]["Options"]
    assert (options["mode"], options["max-buffer-size"], options["awslogs-stream-prefix"]) == (
        "non-blocking",
        "26214400b",
        "pds",
    )
    [group] = template.find_resources("AWS::Logs::LogGroup").values()
    assert group["Properties"] == {"LogGroupName": "/aws/ecs/fastcid-pds", "RetentionInDays": 7}
    assert group["DeletionPolicy"] == "Delete"


def test_the_task_role_gets_exactly_the_sidecars_grants() -> None:
    stack = guidance_like()
    wrapper.augment(stack, json.loads(SPEC.model_dump_json()))
    policies = Template.from_stack(stack).find_resources("AWS::IAM::Policy")
    granted = [
        statement
        for name, policy in policies.items()
        if "TaskRole" in name
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]

    assert len(granted) == 2 * len(SPEC.statements), "both task roles"
    assert {json.dumps(s, sort_keys=True) for s in granted} == {json.dumps(s, sort_keys=True) for s in SPEC.statements}


def test_the_s3_endpoint_lets_the_sidecars_reads_through() -> None:
    """The guidance's endpoint policy admits only its own buckets: S3 refused the sidecar's snapshot read until its
    reads joined it (fastcid, 2026-10-30)."""
    stack = guidance_like()
    wrapper.augment(stack, json.loads(SPEC.model_dump_json()))
    endpoints = Template.from_stack(stack).find_resources("AWS::EC2::VPCEndpoint").values()
    [endpoint] = [e for e in endpoints if ".s3" in json.dumps(e["Properties"]["ServiceName"])]
    statements = endpoint["Properties"]["PolicyDocument"]["Statement"]

    assert statements[0]["Resource"] == "arn:aws:s3:::guidance/*", "the guidance's own statement stays first"
    [snapshot] = [s for s in statements if s.get("Sid") == "PdsSnapshot"]
    assert snapshot["Resource"] == "arn:aws:s3:::fastcid-pds-config-0123/snapshot/current.json"
    assert (snapshot["Action"], snapshot["Principal"]) == ("s3:GetObject", {"AWS": "*"})
    assert snapshot["Condition"] == {"StringEquals": {"aws:ResourceAccount": "123456789012"}}


def test_reservations_reach_dynamodb_through_a_gateway_endpoint_for_the_sidecars_tables_only() -> None:
    """The guidance's VPC had no DynamoDB endpoint: each reservation went out through a NAT gateway."""
    stack = guidance_like()
    wrapper.augment(stack, json.loads(SPEC.model_dump_json()))
    endpoints = Template.from_stack(stack).find_resources("AWS::EC2::VPCEndpoint").values()
    [dynamodb] = [e for e in endpoints if "dynamodb" in json.dumps(e["Properties"]["ServiceName"])]

    assert dynamodb["Properties"]["VpcEndpointType"] == "Gateway"
    assert len(dynamodb["Properties"]["RouteTableIds"]) >= 2, "every subnet's route table"
    [statement] = dynamodb["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Resource"] == [
        "arn:aws:dynamodb:us-east-1:123456789012:table/fastcid-pds",
        "arn:aws:dynamodb:us-east-1:123456789012:table/fastcid-ledger",
    ]
    assert "dynamodb:PutItem" in statement["Action"] and "dynamodb:Scan" not in statement["Action"]


def test_prebid_servers_target_groups_ramp_a_new_task_up_over_slow_start() -> None:
    stack = guidance_like()
    wrapper.augment(stack, json.loads(SPEC.model_dump_json()))
    groups = Template.from_stack(stack).find_resources("AWS::ElasticLoadBalancingV2::TargetGroup").values()

    assert len(groups) == 2
    for group in groups:
        attributes = {a["Key"]: a["Value"] for a in group["Properties"]["TargetGroupAttributes"]}
        assert attributes["slow_start.duration_seconds"] == "180"


def test_slow_start_is_refused_with_a_routing_algorithm_it_does_not_work_with() -> None:
    stack = guidance_like()
    for node in stack.node.find_all():
        if isinstance(node, elbv2.ApplicationTargetGroup):
            node.set_attribute("load_balancing.algorithm.type", "least_outstanding_requests")
    with pytest.raises(wrapper.SidecarError, match="least_outstanding_requests"):
        wrapper.augment(stack, json.loads(SPEC.model_dump_json()))


def test_a_stack_that_is_not_the_guidances_is_refused() -> None:
    with pytest.raises(wrapper.SidecarError, match="PrebidTaskDef"):
        wrapper.augment(guidance_like(task_id="Other"), json.loads(SPEC.model_dump_json()))
    bare = Stack(App(), "bare")
    _vpc(bare)
    task = ecs.FargateTaskDefinition(bare, "PrebidTaskDef", cpu=256, memory_limit_mib=512)
    task.add_container("Other-Container", image=ecs.ContainerImage.from_registry("other"))
    with pytest.raises(wrapper.SidecarError, match="Prebid-Container"):
        wrapper.augment(bare, json.loads(SPEC.model_dump_json()))
    no_endpoint = Stack(App(), "no-endpoint")
    _Entry(no_endpoint, "CloudFrontEntryDeployment", "CloudFront")
    with pytest.raises(wrapper.SidecarError, match="S3 gateway endpoint"):
        wrapper.augment(no_endpoint, json.loads(SPEC.model_dump_json()))
    assert not Template.from_stack(no_endpoint).find_resources("AWS::Logs::LogGroup"), "nothing added on refusal"


def test_the_deploy_command_names_the_wrapper_only_with_a_sidecar() -> None:
    assert demand.GUIDANCE_APP == SCRIPTS / "guidance_app.py"
    assert CONTEXT == wrapper.CONTEXT


@pytest.mark.skipif(
    not (CHECKOUT / guidance.APPLIED_MARKER).is_file(),
    reason="needs `uv run python scripts/prebid_guidance.py apply` (network) to fill build/prebid-guidance/checkout",
)
def test_the_real_guidance_synthesizes_with_the_sidecar(tmp_path: Path) -> None:
    """The pinned guidance, in its own Python 3.11 and aws-cdk-lib, through the wrapper as ``cdk deploy`` runs it:
    both task definitions get the sidecar, and everything else is the guidance's."""
    venv = demand.guidance_toolchain(CHECKOUT)
    context = {
        "stackPrefix": "fastcid",
        "bootstrapQualifier": "fastcid",
        "fileAssetsBucketName": "fastcid-cdk-assets-123456789012-us-east-1",
        "imageAssetsRepositoryName": "fastcid-cdk-images-123456789012-us-east-1",
        "deployBiddingSimulator": "false",
        CONTEXT: SPEC.model_dump_json(),
    }
    environment = {
        **os.environ,
        "CDK_OUTDIR": str(tmp_path),
        "CDK_CONTEXT_JSON": json.dumps(context),
        "OVERRIDE_ECR_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com/fastcid-pbs:3.43.0-0123",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
        "VIRTUAL_ENV": str(venv),
        "PATH": f"{venv / 'bin'}:{os.environ['PATH']}",
    }
    subprocess.run(
        [str(venv / "bin" / "python"), str(SCRIPTS / "guidance_app.py")],
        cwd=CHECKOUT / "source" / "infrastructure",
        env=environment,
        check=True,
        capture_output=True,
        timeout=600,
    )
    template = json.loads((tmp_path / "prebid-server-deployment-on-aws.template.json").read_text())
    resources = template["Resources"]
    definitions = {k: r for k, r in resources.items() if r["Type"] == "AWS::ECS::TaskDefinition"}

    assert {r.get("Condition") for r in definitions.values()} == {
        "DeployCloudFrontWafCondition",
        "DeployALBHttpsCondition",
    }
    for definition in definitions.values():
        properties = definition["Properties"]
        assert (properties["Cpu"], properties["Memory"]) == ("2048", "6144")
        names = {c["Name"]: c for c in properties["ContainerDefinitions"]}
        assert set(names) == {"Prebid-Container", "Pds-Container"}
        assert names["Prebid-Container"]["DependsOn"] == [{"Condition": "HEALTHY", "ContainerName": "Pds-Container"}]
        assert names["Pds-Container"]["Image"] == SPEC.image
    endpoints = [r for r in resources.values() if r["Type"] == "AWS::EC2::VPCEndpoint"]
    [s3] = [e for e in endpoints if ".s3" in json.dumps(e["Properties"]["ServiceName"])]
    sids = [s.get("Sid") for s in s3["Properties"]["PolicyDocument"]["Statement"]]
    assert sids[-1] == "PdsSnapshot" and len(sids) == 4, "the guidance's three statements, then the sidecar's read"
    [dynamodb] = [e for e in endpoints if "dynamodb" in json.dumps(e["Properties"]["ServiceName"])]
    assert [s["Sid"] for s in dynamodb["Properties"]["PolicyDocument"]["Statement"]] == ["PdsTables"]
    groups = [
        {a["Key"]: a["Value"] for a in r["Properties"].get("TargetGroupAttributes", [])}
        for k, r in resources.items()
        if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup" and "ALBTargetGroup" in k
    ]
    assert len(groups) == 2 and all(g["slow_start.duration_seconds"] == "180" for g in groups), groups
    for definition in definitions.values():
        cpus = {c["Name"]: c.get("Cpu") for c in definition["Properties"]["ContainerDefinitions"]}
        assert cpus == {"Prebid-Container": 1536, "Pds-Container": 512}
    [group] = [r for k, r in resources.items() if r["Type"] == "AWS::Logs::LogGroup" and k.startswith("PdsSidecarLogs")]
    assert group["Properties"]["LogGroupName"] == "/aws/ecs/fastcid-pds" and "Condition" not in group
