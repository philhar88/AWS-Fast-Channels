"""Agentic selling (M7): the seller, the buyer and governance agents and the sync function, beside the guidance."""

import json
import string
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.selling.names import CONTEXT, Image
from fast_channels.core.selling.products import Terms

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
    "container_log_group": "fastm-pbs-PrebidContainerLogGroup",
}
IMAGES = {
    image.value: f"123456789012.dkr.ecr.us-east-1.amazonaws.com/fastm-{image.value}:0123456789abcdef" for image in Image
}
SELLING_CONTEXT = {
    "images": IMAGES,
    "catalog_table": "FastChannels-fastm-Data-Catalog",
    "origin_facing_prefix_list": "pl-3b927c52",
}
EXTRA = {NETWORK_CONTEXT: NETWORK, CONTEXT: SELLING_CONTEXT}
SELLING = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
    "modules.selling.enabled": True,
    "modules.selling.cpm": 40,
}
SYNC = "fast_channels.functions.selling.sync.handler"


def resources(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template.find_resources(kind).items()}


def test_the_selling_stack_exists_only_with_the_module_and_its_context(deployment: GetDeployment) -> None:
    assert deployment(SELLING, extra={NETWORK_CONTEXT: NETWORK}).stage.selling is None
    stage = deployment(SELLING, extra=EXTRA).stage

    assert stage.selling.stack_name == "FastChannels-fastm-Selling"
    assert {s.stack_name for s in stage.selling.dependencies} == {
        "FastChannels-fastm-Pds",
        "FastChannels-fastm-Release",
    }
    assert deployment({**SELLING, "modules.selling.enabled": False}, extra=EXTRA).stage.selling is None


def test_the_sync_function_knows_what_the_stage_sells(deployment: GetDeployment) -> None:
    d = deployment(SELLING, extra=EXTRA)
    template = d.template(d.stage.selling)
    [sync] = [f for f in resources(template, "AWS::Lambda::Function").values() if f["Handler"] == SYNC]

    variables = sync["Environment"]["Variables"]
    terms = Terms.model_validate_json(variables["SELLING_PRODUCTS"])
    assert (terms.channels, terms.cpm, terms.creatives) == (("fastm-demo",), 40, ("direct2",))
    assert terms.breaks_ms == (30030, 60060, 90090, 120120)
    assert variables["PDS_BOOKED_LINES_KEY"] == "lines/booked.json"
    assert variables["CATALOG_TABLE"] == "FastChannels-fastm-Data-Catalog"


def test_the_seller_runs_the_pinned_image_with_its_bridge(deployment: GetDeployment) -> None:
    d = deployment(SELLING, extra=EXTRA)
    template = d.template(d.stage.selling)
    [task] = resources(template, "AWS::ECS::TaskDefinition").values()

    containers = {c["Name"]: c for c in task["ContainerDefinitions"]}
    assert set(containers) == {"app", "bridge"}
    assert containers["app"]["Command"] == ["serve"] and containers["bridge"]["Command"] == ["bridge"]
    assert containers["bridge"]["Essential"] is False
    assert containers["bridge"]["DependsOn"] == [{"ContainerName": "app", "Condition": "HEALTHY"}]
    environment = {e["Name"]: e["Value"] for e in containers["app"]["Environment"]}
    assert environment["ADCP_TESTING"] == environment["ADCP_MULTI_TENANT"] == "true"
    assert environment["CREATIVE_AGENT_URL"].endswith(".invalid")
    assert task["RuntimePlatform"]["CpuArchitecture"] == "ARM64"
    [distribution] = resources(template, "AWS::CloudFront::Distribution").values()
    [origin] = distribution["DistributionConfig"]["Origins"]
    assert origin["OriginCustomHeaders"] == [{"HeaderName": "Fly-Forwarded-Proto", "HeaderValue": "https"}]
    assert "VpcOriginConfig" in origin
    [database] = resources(template, "AWS::RDS::DBInstance").values()
    assert (database["DBInstanceClass"], database["BackupRetentionPeriod"]) == ("db.t4g.micro", 0)


def test_the_database_password_needs_no_url_encoding(deployment: GetDeployment) -> None:
    """The seller does not decode the password in ``DATABASE_URL`` (W-8 A12): a ``^`` (``%5E``) failed a deploy."""
    d = deployment(SELLING, extra=EXTRA)
    template = d.template(d.stage.selling)
    [generated] = [
        s["GenerateSecretString"]
        for s in resources(template, "AWS::SecretsManager::Secret").values()
        if "salesagent" in s["GenerateSecretString"].get("SecretStringTemplate", "")
    ]
    assert generated["GenerateStringKey"] == "password"
    excluded = set(generated["ExcludeCharacters"])
    drawn = set(string.ascii_letters + string.digits + string.punctuation) - excluded
    assert drawn == set(string.ascii_letters + string.digits + "-._")
    assert all(quote(c, safe="") == c for c in drawn)


def test_the_agents_run_on_agentcore_from_the_pinned_images(deployment: GetDeployment) -> None:
    d = deployment(SELLING, extra=EXTRA)
    template = d.template(d.stage.selling)
    runtimes = {r["AgentRuntimeName"]: r for r in resources(template, "AWS::BedrockAgentCore::Runtime").values()}

    assert set(runtimes) == {"fastm_buyer", "fastm_governance"}
    buyer, governance = runtimes["fastm_buyer"], runtimes["fastm_governance"]
    assert buyer["ProtocolConfiguration"] == "A2A" and "AuthorizerConfiguration" not in buyer
    assert governance["ProtocolConfiguration"] == "MCP"
    assert "CustomJWTAuthorizer" in governance["AuthorizerConfiguration"]
    text = json.dumps(buyer["EnvironmentVariables"])
    assert '\\"a2a_version\\":\\"1.0\\"' in text and "m2m_oauth" in text
    assert "Ref" in buyer["EnvironmentVariables"]["SESSIONS_TABLE_NAME"]
    assert buyer["EnvironmentVariables"]["BEDROCK_MODEL_ID"] == "us.anthropic.claude-haiku-5-5"
    [key] = resources(template, "AWS::KMS::Key").values()
    assert (key["KeySpec"], key["KeyUsage"]) == ("ECC_NIST_P256", "SIGN_VERIFY")


def test_the_publisher_adds_the_booked_lines_only_with_the_module(deployment: GetDeployment) -> None:
    def publisher(overrides: dict[str, Any]) -> dict[str, str]:
        d = deployment(overrides, extra=EXTRA)
        functions = resources(d.template(d.stage.pds), "AWS::Lambda::Function").values()
        [f] = [f for f in functions if f["Handler"] == "fast_channels.functions.pds.publisher.handler"]
        return dict(f["Environment"]["Variables"])

    assert publisher(SELLING)["PDS_BOOKED_LINES_KEY"] == "lines/booked.json"
    assert "PDS_BOOKED_LINES_KEY" not in publisher({**SELLING, "modules.selling.enabled": False})


@pytest.mark.parametrize("stack", ["selling", "demand_build"])
def test_template_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion, stack: str
) -> None:
    """What the module adds: the Selling stack, and the images' repositories and build project in DemandBuild."""
    d = deployment(SELLING, extra=EXTRA)

    assert normalized(d.template(getattr(d.stage, stack))) == snapshot_json
