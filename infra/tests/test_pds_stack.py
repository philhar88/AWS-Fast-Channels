"""The Pod Decision Service (M2-02 to M2-04): its state in the Data stack and the Pds stack beside the guidance."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.pds.records import DeliveryRecord, PolicyRecord
from fast_channels.core.pds.snapshot import SnapshotBase
from fast_channels.infra.constructs.pds.decisions import LOG_FIELDS

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
EXTRA = {NETWORK_CONTEXT: NETWORK}
PDS = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
}
LINES = [
    {
        "id": "direct-1",
        "advertiser": "Direct Advertiser 1",
        "adomain": "direct1.example",
        "creatives": ["direct1"],
        "cpm": 8,
        "budget": 100,
        "tier0": True,
    },
    {
        "id": "direct-2",
        "advertiser": "Direct Advertiser 2",
        "adomain": "direct2.example",
        "creatives": ["direct2"],
        "cpm": 6,
        "budget": 1,
        "caps": {"per_session": 4},
    },
]
DELIVERY = "fast_channels.functions.pds.delivery.handler"
SWEEPER = "fast_channels.functions.pds.sweeper.handler"
SIDECAR_LOGS = "/aws/ecs/fastm-pds"


def resources(template: Template, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template.find_resources(kind).items()}


def only(template: Template, kind: str) -> dict[str, Any]:
    [properties] = resources(template, kind).values()
    return properties


def function(template: Template, handler: str) -> tuple[str, dict[str, Any]]:
    [(name, properties)] = [
        (name, f) for name, f in resources(template, "AWS::Lambda::Function").items() if f["Handler"] == handler
    ]
    return name, properties


def statements(template: Template, role_prefix: str) -> list[dict[str, Any]]:
    return [
        statement
        for name, p in resources(template, "AWS::IAM::Policy").items()
        if name.startswith(role_prefix)
        for statement in p["PolicyDocument"]["Statement"]
    ]


def test_the_pds_stack_reads_the_data_stack_by_parameter_not_by_weak_reference(deployment: GetDeployment) -> None:
    """The Data stack is also deployed without the guidance's network (make deploy), so it must not owe the Pds
    stack any output."""
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds).to_json()

    text = json.dumps(template)
    assert "FastChannels-fastm-Data" not in text, "no weak reference to the Data stack"
    assert "FastChannels-fastm-Media" not in text, "nor to the Media stack"
    ssm = {p["Default"] for p in template["Parameters"].values() if p["Type"].startswith("AWS::SSM::Parameter")}
    assert ssm - {"/cdk-bootstrap/hnb659fds/version"} == {
        "/fastm/pds/config-bucket",
        "/fastm/pds/lake-bucket",
        "/fastm/pds/media-bucket",
        "/fastm/pds/content-base-url",
    }
    plain = deployment(PDS)
    for stack in ("data", "media"):
        assert plain.template(getattr(plain.stage, stack)).to_json() == d.template(getattr(d.stage, stack)).to_json()


def test_the_pds_stack_exists_only_with_the_module_and_the_guidances_network(deployment: GetDeployment) -> None:
    assert deployment(PDS).stage.pds is None
    stage = deployment(PDS, extra=EXTRA).stage

    assert stage.pds.stack_name == "FastChannels-fastm-Pds"
    assert {s.stack_name for s in stage.pds.dependencies} == {"FastChannels-fastm-Data", "FastChannels-fastm-Ops"}
    assert deployment({**PDS, "modules.pds.enabled": False}, extra=EXTRA).stage.pds is None


def test_the_state_is_the_data_stacks(deployment: GetDeployment) -> None:
    d = deployment(PDS)
    template = d.template(d.stage.data)
    tables = resources(template, "AWS::DynamoDB::GlobalTable")
    [table] = [t for t in tables.values() if t.get("TableName") == "fastm-pds"]

    assert [k["AttributeName"] for k in table["KeySchema"]] == ["pk", "sk"]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    assert table["Replicas"][0]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
    [index] = table["GlobalSecondaryIndexes"]
    assert index["IndexName"] == "pending"
    assert [k["AttributeName"] for k in index["KeySchema"]] == ["pending", "pending_at"]
    assert index["Projection"] == {"ProjectionType": "KEYS_ONLY"}
    outputs = template.to_json()["Outputs"]
    assert {"PdsTableName", "PdsConfigBucketName"} <= set(outputs)
    parameters = {p["Name"] for p in resources(template, "AWS::SSM::Parameter").values()}
    assert {"/fastm/pds/config-bucket", "/fastm/pds/lake-bucket", "/fastm/pds/media-bucket"} <= parameters
    media = {p["Name"] for p in resources(d.template(d.stage.media), "AWS::SSM::Parameter").values()}
    assert {"/fastm/pds/content-base-url", "/fastm/pds/ladder-version"} <= media
    off = deployment({**PDS, "modules.pds.enabled": False})
    assert "PdsTableName" not in off.template(off.stage.data).to_json()["Outputs"]
    off_media = resources(off.template(off.stage.media), "AWS::SSM::Parameter").values()
    assert not [p for p in off_media if p["Name"].startswith("/fastm/pds/")]


def test_the_sidecar_answers_prebid_server_so_no_endpoint_is_here(deployment: GetDeployment) -> None:
    """``/policy``, ``/select`` and ``/ownbook/openrtb`` are the sidecar's, in the guidance's task (ADR-0036): no
    load balancer, record, alias, provisioned-concurrency gate or hot-path function is left, nor the guidance's VPC."""
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    text = json.dumps(template.to_json())
    handlers = {f["Handler"] for f in resources(template, "AWS::Lambda::Function").values()}

    for kind in (
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        "AWS::Route53::RecordSet",
        "AWS::Lambda::Alias",
        "AWS::StepFunctions::StateMachine",
        "AWS::EC2::SecurityGroup",
    ):
        assert not template.find_resources(kind), kind
    assert not [h for h in handlers if h.split(".")[-2] in {"policy", "select", "ownbook"}], handlers
    assert NETWORK["vpc_id"] not in text and "subnet-" not in text
    outputs = template.to_json()["Outputs"]
    assert {"DecisionLogGroup", "DeliveryFunction", "PublisherFunction"} <= set(outputs)
    assert not {"Endpoint", "PolicyFunction", "SelectFunction", "OwnBookFunction", "CensusGateArn"} & set(outputs)


def test_the_sidecars_decision_records_are_subscribed_on_its_log_group(deployment: GetDeployment) -> None:
    """Both of the log group's two subscription filters: one per record kind, each to its stream."""
    d = deployment(PDS, extra=EXTRA)
    filters = resources(d.template(d.stage.pds), "AWS::Logs::SubscriptionFilter").values()
    sidecar = {f["FilterName"]: f["FilterPattern"] for f in filters if f["LogGroupName"] == SIDECAR_LOGS}

    assert sidecar == {
        "fastm-pds-policy-records": '{ $.record = "pds.policy" }',
        "fastm-pds-select-records": '{ $.record = "pds.select" }',
    }


def test_the_publisher_runs_on_deploy_and_hourly_with_the_demo_demand(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    functions = resources(template, "AWS::Lambda::Function")
    [publisher] = [f for f in functions.values() if f["Handler"] == "fast_channels.functions.pds.publisher.handler"]

    base = SnapshotBase.model_validate_json(publisher["Environment"]["Variables"]["PDS_SNAPSHOT_BASE"])
    assert base.demand == {"amt": {"placementId": "fastm-demo"}}
    assert base.lines == ()
    assert base.breaks_ms == (30030, 60060, 90090, 120120), "the 30.03 s grid of the default ladder"
    schedules = {s["ScheduleExpression"] for s in resources(template, "AWS::Scheduler::Schedule").values()}
    assert schedules == {"rate(1 hour)", "rate(15 minutes)"}
    assert template.find_resources("Custom::Trigger")
    lined = deployment({**PDS, "modules.pds.demo_lines": LINES}, extra=EXTRA)
    _, publisher = function(lined.template(lined.stage.pds), "fast_channels.functions.pds.publisher.handler")
    base = SnapshotBase.model_validate_json(publisher["Environment"]["Variables"]["PDS_SNAPSHOT_BASE"])
    assert [(line.id, line.tier0, line.per_session) for line in base.lines] == [
        ("direct-1", True, None),
        ("direct-2", False, 4),
    ]
    assert set(publisher["Environment"]["Variables"]) >= {"MEDIA_BUCKET", "PDS_TABLE", "LEDGER_TABLE"}
    own_book = deployment({**PDS, "modules.demand.mode": "own_book"}, extra=EXTRA)
    [quiet] = [
        f
        for f in resources(own_book.template(own_book.stage.pds), "AWS::Lambda::Function").values()
        if f["Handler"] == "fast_channels.functions.pds.publisher.handler"
    ]
    assert SnapshotBase.model_validate_json(quiet["Environment"]["Variables"]["PDS_SNAPSHOT_BASE"]).demand == {}


def test_the_publisher_reads_only_the_kits_and_writes_only_budgets(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    granted = statements(d.template(d.stage.pds), "PublisherFunctionRole")
    by_action = {json.dumps(s["Action"]): s for s in granted}

    kits = json.dumps([s["Resource"] for s in granted if s["Action"] == "s3:GetObject"])
    assert "ads/*/vast.xml" in kits and "ads/*/demo/creatives.json" in kits
    table = by_action['["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]']
    assert table["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["BUDGET#*"]}}
    ledger = [s for s in granted if s["Action"] == "dynamodb:PutItem"]
    assert [s["Condition"] for s in ledger] == [{"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["pds/*"]}}]
    parameter = by_action['"ssm:GetParameter"']
    assert "parameter/fastm/pds/ladder-version" in json.dumps(parameter["Resource"])


def test_delivery_reads_the_impression_beacons_of_the_stages_mediatailor_log(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    name, delivery = function(template, DELIVERY)
    filters = resources(template, "AWS::Logs::SubscriptionFilter")
    [beacons] = [f for f in filters.values() if f.get("LogGroupName") == "/aws/vendedlogs/mediatailor/fastm"]

    assert beacons["FilterPattern"] == (
        '{ ($.eventType = "BEACON_FIRED") && ($.beaconInfo.trackingEvent = "impression") }'
    )
    assert beacons["FilterName"] == "fastm-pds-delivery"
    assert set(delivery["Environment"]["Variables"]) >= {"PDS_TABLE", "LEDGER_TABLE", "CONTENT_BASE_URL"}
    [invoke] = [
        c for c in resources(template, "AWS::Lambda::EventInvokeConfig").values() if c["FunctionName"] == {"Ref": name}
    ]
    assert invoke["MaximumRetryAttempts"] == 2
    assert "OnFailure" in invoke["DestinationConfig"]
    keys = sorted(
        (json.dumps(s["Action"]), s["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"])
        for s in statements(template, name[:-8] + "Role")
        if "Condition" in s
    )
    assert keys == [
        ('"dynamodb:PutItem"', ["IMP#*", "FILL#*"]),
        ('"dynamodb:PutItem"', ["pds/*"]),
        ('["dynamodb:GetItem", "dynamodb:UpdateItem"]', ["RES#*", "BUDGET#*", "CAP#*"]),
    ]
    names = {a["AlarmName"] for a in resources(template, "AWS::CloudWatch::Alarm").values()}
    assert {"fastm-pds-delivery-dead-letters", "fastm-pds-sweeper-dead-letters"} <= names


def test_the_sweeper_queries_only_the_pending_index(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    name, sweeper = function(template, SWEEPER)

    assert sweeper["Timeout"] == 300
    [query] = [s for s in statements(template, name[:-8] + "Role") if s["Action"] == "dynamodb:Query"]
    assert query["Resource"] == "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-pds/index/pending"


def test_policy_records_flow_into_the_lake(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    streams = resources(template, "AWS::KinesisFirehose::DeliveryStream")
    [stream] = [s for s in streams.values() if s["DeliveryStreamName"] == "fastm-pds-policy"]
    destination = stream["ExtendedS3DestinationConfiguration"]
    filters = resources(template, "AWS::Logs::SubscriptionFilter").values()
    [subscription] = [f for f in filters if f["FilterPattern"] == '{ $.record = "pds.policy" }']
    tables = [t["TableInput"] for t in resources(template, "AWS::Glue::Table").values()]
    [table] = [t for t in tables if t["Name"] == "pds_policy"]

    assert stream["DeliveryStreamName"] == "fastm-pds-policy"
    assert destination["Prefix"] == "pds/policy/!{timestamp:yyyy/MM/dd/HH}/"
    assert [p["Type"] for p in destination["ProcessingConfiguration"]["Processors"]] == [
        "Decompression",
        "CloudWatchLogProcessing",
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
    [delivery] = [t for t in tables if t["Name"] == "pds_delivery"]
    columns = {c["Name"]: c["Type"] for c in delivery["StorageDescriptor"]["Columns"]}
    assert set(columns) == set(DeliveryRecord.model_fields) | {name for name, _ in LOG_FIELDS}
    assert (columns["impression"], columns["fill"], columns["enr"]) == ("boolean", "boolean", "bigint")
    assert {s["DeliveryStreamName"] for s in streams.values()} == {
        "fastm-pds-policy",
        "fastm-pds-select",
        "fastm-pds-delivery",
    }
    [select] = [t for t in tables if t["Name"] == "pds_select"]
    columns = {c["Name"]: c["Type"] for c in select["StorageDescriptor"]["Columns"]}
    assert (columns["reserved"], columns["reason"], columns["below_floor"]) == ("array<string>", "string", "bigint")


@pytest.mark.parametrize("overrides", [PDS], ids=["pds"])
def test_template_snapshot(
    deployment: GetDeployment,
    normalized: Callable[..., Any],
    snapshot_json: SnapshotAssertion,
    overrides: dict[str, Any],
) -> None:
    d = deployment(overrides, extra=EXTRA)

    assert normalized(d.template(d.stage.pds)) == snapshot_json


def test_the_module_fallback_alarms_watch_prebid_servers_log(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    filters = {
        f["MetricTransformations"][0]["MetricName"]: f for f in resources(template, "AWS::Logs::MetricFilter").values()
    }

    assert set(filters) == {
        "Pods",
        "PolicyFallbacks",
        "SelectFallbacks",
        "PodErrors",
        "HouseFromConfig",
        "PodsRejected",
        "PodsRejectedOutOfRange",
        "PodsRejectedUnreadable",
    }
    assert filters["PodsRejectedOutOfRange"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "REJECTED OUT_OF_RANGE"'
    assert {f["LogGroupName"] for f in filters.values()} == {"fastm-pbs-PrebidContainerLogGroup"}
    assert filters["Pods"]["FilterPattern"] == '"fast-ctv-pod" -"warmup"'
    assert filters["PolicyFallbacks"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "POLICY_"'
    assert filters["SelectFallbacks"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "SELECT_FALLBACK"'
    assert filters["HouseFromConfig"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "HOUSE_CONFIG"'
    assert {f["MetricTransformations"][0]["MetricNamespace"] for f in filters.values()} == {
        "FastChannels/PrebidServer/fastm"
    }
    alarms = {a["AlarmName"]: a for a in resources(template, "AWS::CloudWatch::Alarm").values()}
    policy, select = alarms["fastm-pds-policy-fallbacks"], alarms["fastm-pds-select-fallbacks"]
    assert (policy["Threshold"], select["Threshold"]) == (1, 0.5)
    assert policy["ComparisonOperator"] == "GreaterThanThreshold"
    [expression] = [m["Expression"] for m in policy["Metrics"] if "Expression" in m]
    assert expression == "IF(pods >= 20, 100 * (FILL(f0, 0)) / pods, IF(FILL(f0, 0) >= 5, 100, 0))"
    [expression] = [m["Expression"] for m in select["Metrics"] if "Expression" in m]
    assert "FILL(f0, 0) + FILL(f1, 0)" in expression, "house fallbacks and module errors"

    without = deployment(PDS, extra={NETWORK_CONTEXT: {**NETWORK, "container_log_group": None}})
    assert not resources(without.template(without.stage.pds), "AWS::Logs::MetricFilter")
