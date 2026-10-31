"""The demand module beside the guidance (M8-04): the Demand stack (private zone, ``lambda`` simulator), the demo
creatives in the ladder kit, and the rule that no demand role writes to the ingest bucket."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.demo_demand import DEMO_CREATIVES
from fast_channels.infra.constructs.encoding.kits import DEMO_DIR, brand_digest

if TYPE_CHECKING:
    from aws_cdk.assertions import Template
    from syrupy.assertion import SnapshotAssertion

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
LAMBDA = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
}
GUIDANCE_STACK = {**LAMBDA, "modules.demand.simulator": "guidance_stack"}
OWN_BOOK = {**LAMBDA, "modules.demand.mode": "own_book"}


def resources(template: Template, kind: str) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template.find_resources(kind).values()]


def test_the_demand_stack_exists_only_with_the_guidances_network(deployment: GetDeployment) -> None:
    assert deployment(LAMBDA).stage.demand is None
    stage = deployment(LAMBDA, extra=EXTRA).stage

    assert stage.demand.stack_name == "FastChannels-fastm-Demand"
    assert not stage.demand.dependencies
    assert deployment({"prefix": "fastm"}, extra=EXTRA).stage.demand is None, "only with modules.demand"


def test_the_network_may_come_as_a_json_string(deployment: GetDeployment) -> None:
    stage = deployment(LAMBDA, extra={NETWORK_CONTEXT: json.dumps(NETWORK)}).stage

    assert stage.demand is not None


def test_the_private_zone_is_in_the_guidances_vpc(deployment: GetDeployment) -> None:
    d = deployment(GUIDANCE_STACK, extra=EXTRA)
    template = d.template(d.stage.demand)
    [zone] = resources(template, "AWS::Route53::HostedZone")

    assert zone["Name"] == "fastm.fast.internal."
    assert zone["VPCs"] == [{"VPCId": NETWORK["vpc_id"], "VPCRegion": "us-east-1"}]
    assert not template.find_resources("AWS::Lambda::Function"), "guidance_stack: the guidance's simulator stack"


@pytest.mark.parametrize("overrides", [LAMBDA], ids=["lambda"])
def test_template_snapshot(
    deployment: GetDeployment,
    normalized: Callable[..., Any],
    snapshot_json: SnapshotAssertion,
    overrides: dict[str, Any],
) -> None:
    d = deployment(overrides, extra=EXTRA)

    assert normalized(d.template(d.stage.demand)) == snapshot_json


def test_the_lambda_simulator_answers_only_prebid_server_at_sim(deployment: GetDeployment) -> None:
    d = deployment(LAMBDA, extra=EXTRA)
    template = d.template(d.stage.demand)
    [function] = resources(template, "AWS::Lambda::Function")
    [balancer] = resources(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    [record] = resources(template, "AWS::Route53::RecordSet")
    [ingress] = resources(template, "AWS::EC2::SecurityGroupIngress") or [
        rule
        for group in resources(template, "AWS::EC2::SecurityGroup")
        for rule in group.get("SecurityGroupIngress", [])
    ]

    assert function["FunctionName"] == "fastm-demand-simulator"
    assert function["Handler"] == "handler.lambda_handler"
    assert function["Code"]["S3Key"] == NETWORK["simulator_code_key"]
    assert function["Code"]["S3Bucket"] == "fastm-cdk-assets-123456789012-us-east-1"
    assert function["Environment"]["Variables"]["BID_RESPONSES_TIMEOUT_PERCENTAGE"] == "0"
    assert balancer["Scheme"] == "internal"
    assert sorted(balancer["Subnets"]) == sorted(s["id"] for s in NETWORK["private_subnets"])
    assert record["Name"] == "sim.fastm.fast.internal."
    assert (ingress["SourceSecurityGroupId"], ingress["FromPort"], ingress["ToPort"]) == (
        NETWORK["task_security_group_id"],
        80,
        80,
    )


def test_the_lambda_simulator_needs_its_code(deployment: GetDeployment) -> None:
    with pytest.raises(ValueError, match="simulator_code_key"):
        deployment(LAMBDA, extra={NETWORK_CONTEXT: {**NETWORK, "simulator_code_key": None}})


def test_own_book_mode_has_no_simulator(deployment: GetDeployment) -> None:
    d = deployment(OWN_BOOK, extra=EXTRA)

    assert d.stage.demand.simulator is None


def test_demo_kits_encode_the_demo_creatives_from_file_assets(deployment: GetDeployment) -> None:
    on = deployment(LAMBDA).stage.media
    off = deployment(OWN_BOOK).stage.media

    assert on.provisioner.demo_digest == brand_digest(DEMO_DIR)
    assert off.provisioner.demo_digest is None
    demo = on.provisioner.images["demo"]
    assert sorted(demo) == sorted(c.crid for c in DEMO_CREATIVES)
    assert {(DEMO_DIR / f"{c.crid}.png").is_file() for c in DEMO_CREATIVES} == {True}
    template = deployment(LAMBDA).template(on)
    [kit] = [r["Properties"] for r in template.find_resources("Custom::LadderKit").values()]
    assert kit["DemoDigest"] == brand_digest(DEMO_DIR), "turning demo mode on re-provisions the kit"


INGEST = ("StorageIngest", "IngestBucket")
"""How a template names the ingest bucket: the Data stack's storage construct, or the weak references to it."""


def ingest_bucket_writes(template: Template) -> list[str]:
    """The IAM policies of a template that may write objects to the ingest bucket."""
    found = []
    for kind in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"):
        for logical, resource in template.find_resources(kind).items():
            properties = resource["Properties"]
            documents = [properties.get("PolicyDocument")] + [
                p["PolicyDocument"] for p in properties.get("Policies", [])
            ]
            for document in filter(None, documents):
                for statement in document["Statement"]:
                    actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
                    writes = [a for a in actions if a in ("s3:*", "*") or a.startswith("s3:Put")]
                    if writes and any(name in json.dumps(statement.get("Resource")) for name in INGEST):
                        found.append(logical)
    return found


def test_no_demand_role_writes_to_the_ingest_bucket(deployment: GetDeployment) -> None:
    """Demo creatives never go through the ingest bucket, whose rule ingests any video file under any key: the
    demand stacks cannot write to it, and demo mode adds no writer to the Media stack (its only writer is the brand
    stills' deployment, as with the module off)."""
    on = deployment(LAMBDA, extra=EXTRA)
    off = deployment({"prefix": "fastm"})

    for stack in (on.stage.demand, on.stage.demand_build, on.stage.demand_edge):
        assert ingest_bucket_writes(on.template(stack)) == [], stack.stack_name
    assert ingest_bucket_writes(on.template(on.stage.media)) == ingest_bucket_writes(off.template(off.stage.media))
    assert len(ingest_bucket_writes(off.template(off.stage.media))) == 1


def test_mediaconvert_reads_each_demo_still_and_nothing_else_new(deployment: GetDeployment) -> None:
    d = deployment(LAMBDA)
    media = d.template(d.stage.media)
    reads = [
        statement
        for policy in resources(media, "AWS::IAM::Policy")
        for statement in policy["PolicyDocument"]["Statement"]
        if statement["Action"] == "s3:GetObject" and ".png" in json.dumps(statement["Resource"])
    ]

    stills = [r for statement in reads for r in statement["Resource"] if isinstance(r, str) and r.endswith(".png")]
    assert len(set(stills)) == len(DEMO_CREATIVES), "one object each, no wildcard"
    assert all(resource.startswith("arn:aws:s3:::cdk-") for resource in stills)
