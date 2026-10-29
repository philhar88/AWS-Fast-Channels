"""The Pod Decision Service (M2-02): its state in the Data stack and the Pds stack beside the guidance."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.pds.records import PolicyRecord
from fast_channels.core.pds.snapshot import SNAPSHOT_KEY, SnapshotBase
from fast_channels.infra.constructs.pds.decisions import LOG_FIELDS
from fast_channels.infra.constructs.pds.service import census_gate_definition

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
PDS = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
}


def resources(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template.find_resources(kind).items()}


def only(template: Template, kind: str) -> dict[str, Any]:
    [properties] = resources(template, kind).values()
    return properties


def test_the_pds_stack_reads_the_data_stack_by_parameter_not_by_weak_reference(deployment: GetDeployment) -> None:
    """The Data stack is also deployed without the guidance's network (make deploy), so it must not owe the Pds
    stack any output."""
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds).to_json()

    text = json.dumps(template)
    assert "FastChannels-fastm-Data" not in text, "no weak reference to the Data stack"
    assert '"StackName": "FastChannels-fastm-Demand"' in text, "the zone: Demand and Pds always deploy together"
    ssm = {p["Default"] for p in template["Parameters"].values() if p["Type"].startswith("AWS::SSM::Parameter")}
    assert ssm - {"/cdk-bootstrap/hnb659fds/version"} == {"/fastm/pds/config-bucket", "/fastm/pds/lake-bucket"}
    plain = deployment(PDS)
    assert plain.template(plain.stage.data).to_json() == d.template(d.stage.data).to_json(), "the same Data stack"


def test_the_pds_stack_exists_only_with_the_module_and_the_guidances_network(deployment: GetDeployment) -> None:
    assert deployment(PDS).stage.pds is None
    stage = deployment(PDS, extra=EXTRA).stage

    assert stage.pds.stack_name == "FastChannels-fastm-Pds"
    assert {s.stack_name for s in stage.pds.dependencies} == {
        "FastChannels-fastm-Data",
        "FastChannels-fastm-Demand",
        "FastChannels-fastm-Ops",
    }
    assert deployment({**PDS, "modules.pds.enabled": False}, extra=EXTRA).stage.pds is None


def test_the_state_is_the_data_stacks(deployment: GetDeployment) -> None:
    d = deployment(PDS)
    template = d.template(d.stage.data)
    tables = resources(template, "AWS::DynamoDB::GlobalTable")
    [table] = [t for t in tables.values() if t.get("TableName") == "fastm-pds"]

    assert [k["AttributeName"] for k in table["KeySchema"]] == ["pk", "sk"]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert table["Replicas"][0]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    outputs = template.to_json()["Outputs"]
    assert {"PdsTableName", "PdsConfigBucketName"} <= set(outputs)
    parameters = {p["Name"] for p in resources(template, "AWS::SSM::Parameter").values()}
    assert {"/fastm/pds/config-bucket", "/fastm/pds/lake-bucket"} <= parameters
    off = deployment({**PDS, "modules.pds.enabled": False})
    assert "PdsTableName" not in off.template(off.stage.data).to_json()["Outputs"]


def test_the_load_balancer_answers_only_prebid_server_at_pds(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    balancer = only(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    ingress = only(template, "AWS::EC2::SecurityGroupIngress")
    record = only(template, "AWS::Route53::RecordSet")
    listener = only(template, "AWS::ElasticLoadBalancingV2::Listener")
    rule = only(template, "AWS::ElasticLoadBalancingV2::ListenerRule")
    group = only(template, "AWS::ElasticLoadBalancingV2::TargetGroup")

    assert balancer["Scheme"] == "internal"
    assert sorted(balancer["Subnets"]) == sorted(s["id"] for s in NETWORK["private_subnets"])
    assert (ingress["SourceSecurityGroupId"], ingress["FromPort"], ingress["ToPort"]) == (
        NETWORK["task_security_group_id"],
        80,
        80,
    )
    assert record["Name"] == "pds.fastm.fast.internal."
    assert (listener["Port"], listener["Protocol"]) == (80, "HTTP")
    assert listener["DefaultActions"][0]["FixedResponseConfig"]["StatusCode"] == "404"
    conditions = {c["Field"]: c for c in rule["Conditions"]}
    assert conditions["path-pattern"]["PathPatternConfig"]["Values"] == ["/policy"]
    assert conditions["http-request-method"]["HttpRequestMethodConfig"]["Values"] == ["POST"]
    assert group["TargetType"] == "lambda"
    [alias] = resources(template, "AWS::Lambda::Alias")
    assert group["Targets"] == [{"Id": {"Ref": alias}}]


def test_the_policy_function_is_outside_the_vpc_and_reads_only_the_snapshot(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    functions = resources(template, "AWS::Lambda::Function")
    [policy] = [f for f in functions.values() if f["Handler"] == "fast_channels.functions.pds.policy.handler"]

    assert "VpcConfig" not in policy
    assert (policy["Runtime"], policy["Architectures"], policy["MemorySize"], policy["Timeout"]) == (
        "python3.14",
        ["arm64"],
        1024,
        3,
    )
    assert set(policy["Environment"]["Variables"]) >= {"PDS_CONFIG_BUCKET", "FAST_PREFIX"}
    assert only(template, "AWS::Lambda::Alias")["Name"] == "live"
    statements = [
        statement
        for name, p in resources(template, "AWS::IAM::Policy").items()
        if name.startswith("PolicyFunctionRole")
        for statement in p["PolicyDocument"]["Statement"]
    ]
    s3 = [s for s in statements if str(s["Action"]).startswith("s3:")]
    assert [s["Action"] for s in s3] == ["s3:GetObject"]
    assert json.dumps(s3[0]["Resource"]).endswith(f'/{SNAPSHOT_KEY}"]]}}')


def test_the_publisher_runs_on_deploy_and_hourly_with_the_demo_demand(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    functions = resources(template, "AWS::Lambda::Function")
    [publisher] = [f for f in functions.values() if f["Handler"] == "fast_channels.functions.pds.publisher.handler"]

    base = SnapshotBase.model_validate_json(publisher["Environment"]["Variables"]["PDS_SNAPSHOT_BASE"])
    assert base.demand == {"amt": {"placementId": "fastm-demo"}}
    assert only(template, "AWS::Scheduler::Schedule")["ScheduleExpression"] == "rate(1 hour)"
    assert template.find_resources("Custom::Trigger")
    own_book = deployment({**PDS, "modules.demand.mode": "own_book"}, extra=EXTRA)
    [quiet] = [
        f
        for f in resources(own_book.template(own_book.stage.pds), "AWS::Lambda::Function").values()
        if f["Handler"] == "fast_channels.functions.pds.publisher.handler"
    ]
    assert SnapshotBase.model_validate_json(quiet["Environment"]["Variables"]["PDS_SNAPSHOT_BASE"]).demand == {}


def test_policy_records_flow_into_the_lake(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    stream = only(template, "AWS::KinesisFirehose::DeliveryStream")
    destination = stream["ExtendedS3DestinationConfiguration"]
    subscription = only(template, "AWS::Logs::SubscriptionFilter")
    table = only(template, "AWS::Glue::Table")["TableInput"]

    assert stream["DeliveryStreamName"] == "fastm-pds-policy"
    assert destination["Prefix"] == "pds/policy/!{timestamp:yyyy/MM/dd/HH}/"
    assert [p["Type"] for p in destination["ProcessingConfiguration"]["Processors"]] == [
        "Decompression",
        "CloudWatchLogProcessing",
        "AppendDelimiterToRecord",
    ]
    assert subscription["FilterPattern"] == '{ $.record = "pds.policy" }'
    assert table["Name"] == "pds_policy"
    columns = {c["Name"]: c["Type"] for c in table["StorageDescriptor"]["Columns"]}
    assert set(columns) == set(PolicyRecord.model_fields) | {name for name, _ in LOG_FIELDS}
    assert (columns["probe"], columns["latency_ms"], columns["dur_ms"], columns["bidders"]) == (
        "boolean",
        "double",
        "bigint",
        "array<string>",
    )
    assert (columns["v"], columns["devicetype"], columns["contributed"]) == ("bigint", "bigint", "string")
    assert table["PartitionKeys"] == [{"Name": "datehour", "Type": "string"}]


def test_the_census_gate_warms_the_functions_while_channels_run(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    assert template.find_resources("AWS::StepFunctions::StateMachine")
    rule = only(template, "AWS::Events::Rule")
    assert rule["EventPattern"]["detail-type"] == ["Census Changed"]

    cold = deployment({**PDS, "modules.pds.provisioned_concurrency": 0}, extra=EXTRA)
    assert cold.template(cold.stage.pds).find_resources("AWS::StepFunctions::StateMachine"), "it releases what was set"


def test_without_provisioned_concurrency_the_gate_only_releases() -> None:
    definition = census_gate_definition(functions=["arn:f1"], parameter="/p", concurrency=0)
    states = definition["States"]

    assert not [name for name in states if name.startswith("Provision")]
    assert states["AnyChannel"]["Choices"][0]["Next"] == "Release0"
    assert states["AnyChannel"]["Default"] == "Release0"


def test_census_gate_definition() -> None:
    definition = census_gate_definition(
        functions=["arn:f1", "arn:f2"], parameter="/fastm/ops/running-channels", concurrency=2
    )
    states = definition["States"]

    assert states["Provision0"]["Next"] == "Provision1" and states["Provision1"]["End"]
    assert states["Provision1"]["Arguments"] == {
        "FunctionName": "arn:f2",
        "Qualifier": "live",
        "ProvisionedConcurrentExecutions": 2,
    }
    assert states["Release0"]["Next"] == "Release1" and states["Released0"]["Next"] == "Release1"
    assert states["Release1"]["End"] and states["Released1"]["End"]
    assert states["WaitBeforeRelease"]["Seconds"] == 900


@pytest.mark.parametrize("overrides", [PDS], ids=["pds"])
def test_template_snapshot(
    deployment: GetDeployment,
    normalized: Callable[..., Any],
    snapshot_json: SnapshotAssertion,
    overrides: dict[str, Any],
) -> None:
    d = deployment(overrides, extra=EXTRA)

    assert normalized(d.template(d.stage.pds)) == snapshot_json
