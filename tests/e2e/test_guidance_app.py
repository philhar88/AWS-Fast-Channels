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
from aws_cdk import aws_ecs as ecs
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


class _Entry(Construct):
    """One of the guidance's entry deployments: a task definition ``ECSTask/PrebidTaskDef`` with Prebid Server's
    container, every resource under the deployment's condition."""

    def __init__(self, scope: Construct, id: str, condition: str) -> None:
        super().__init__(scope, id)
        task = ecs.FargateTaskDefinition(Construct(self, "ECSTask"), "PrebidTaskDef", cpu=2048, memory_limit_mib=4096)
        task.add_container("Prebid-Container", image=ecs.ContainerImage.from_registry("prebid"))
        stack = Stack.of(self)
        cfn = stack.node.try_find_child(condition) or CfnCondition(
            stack, condition, expression=Fn.condition_equals("a", "a")
        )
        self.condition = cfn


def guidance_like(*, task_id: str = "PrebidTaskDef") -> Stack:
    stack = Stack(App(), "fastcid-pbs")
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
    assert {"PortMappings", "Cpu", "Memory"}.isdisjoint(pds), "loopback only, the task's CPU and memory shared"
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


def test_a_stack_that_is_not_the_guidances_is_refused() -> None:
    with pytest.raises(wrapper.SidecarError, match="PrebidTaskDef"):
        wrapper.augment(guidance_like(task_id="Other"), json.loads(SPEC.model_dump_json()))
    bare = Stack(App(), "bare")
    task = ecs.FargateTaskDefinition(bare, "PrebidTaskDef", cpu=256, memory_limit_mib=512)
    task.add_container("Other-Container", image=ecs.ContainerImage.from_registry("other"))
    with pytest.raises(wrapper.SidecarError, match="Prebid-Container"):
        wrapper.augment(bare, json.loads(SPEC.model_dump_json()))


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
    [group] = [r for k, r in resources.items() if r["Type"] == "AWS::Logs::LogGroup" and k.startswith("PdsSidecarLogs")]
    assert group["Properties"]["LogGroupName"] == "/aws/ecs/fastcid-pds" and "Condition" not in group
