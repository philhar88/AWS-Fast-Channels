"""The Pod Decision Service (M2-02 to M2-04): its state in the Data stack and the Pds stack beside the guidance."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.pds.records import DeliveryRecord, PolicyRecord
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
OWN_BOOK = "fast_channels.functions.pds.ownbook.handler"
DELIVERY = "fast_channels.functions.pds.delivery.handler"
SWEEPER = "fast_channels.functions.pds.sweeper.handler"
SELECT = "fast_channels.functions.pds.select.handler"


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
    assert '"StackName": "FastChannels-fastm-Demand"' in text, "the zone: Demand and Pds always deploy together"
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


def test_the_load_balancer_answers_only_prebid_server_at_pds(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    balancer = only(template, "AWS::ElasticLoadBalancingV2::LoadBalancer")
    ingress = only(template, "AWS::EC2::SecurityGroupIngress")
    record = only(template, "AWS::Route53::RecordSet")
    listener = only(template, "AWS::ElasticLoadBalancingV2::Listener")
    rules = resources(template, "AWS::ElasticLoadBalancingV2::ListenerRule")
    groups = resources(template, "AWS::ElasticLoadBalancingV2::TargetGroup")

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
    paths = []
    for rule in rules.values():
        conditions = {c["Field"]: c for c in rule["Conditions"]}
        paths.extend(conditions["path-pattern"]["PathPatternConfig"]["Values"])
        assert conditions["http-request-method"]["HttpRequestMethodConfig"]["Values"] == ["POST"]
    assert sorted(paths) == ["/ownbook/openrtb", "/policy", "/select"]
    aliases = resources(template, "AWS::Lambda::Alias")
    assert {a["Name"] for a in aliases.values()} == {"live"}
    assert all(g["TargetType"] == "lambda" for g in groups.values())
    assert sorted(g["Targets"][0]["Id"]["Ref"] for g in groups.values()) == sorted(aliases)
    attributes = {a["Key"]: a["Value"] for a in balancer["LoadBalancerAttributes"]}
    assert attributes["idle_timeout.timeout_seconds"] == "120"  # above Prebid Server's 60 s keep-alive


def joined(value: Any) -> str:
    """A CloudFormation string with its ``Ref``s as ``${name}``."""
    match value:
        case {"Fn::Join": [separator, parts]}:
            return str(separator).join(joined(part) for part in parts)
        case {"Ref": name}:
            return f"${{{name}}}"
        case str():
            return value
    raise AssertionError(f"not a string: {value}")


def test_only_the_stages_own_target_groups_may_invoke_the_functions(deployment: GetDeployment) -> None:
    """CDK's ``LambdaTarget`` lets Elastic Load Balancing invoke a function unconditioned, from any account's target
    group (a confused deputy). Each alias's permission names the account and its target group, by the group's fixed
    name, and exists before the group registers the target."""
    d = deployment(PDS, extra=EXTRA)
    resources_ = d.template(d.stage.pds).to_json()["Resources"]
    groups = {k: r for k, r in resources_.items() if r["Type"] == "AWS::ElasticLoadBalancingV2::TargetGroup"}
    permissions = {
        k: r["Properties"]
        for k, r in resources_.items()
        if r["Type"] == "AWS::Lambda::Permission"
        and r["Properties"]["Principal"] == "elasticloadbalancing.amazonaws.com"
    }

    names = sorted(g["Properties"]["Name"] for g in groups.values())
    assert names == ["fastm-pds-ownbook", "fastm-pds-policy", "fastm-pds-select"]
    account, region = d.stage.pds.account, d.stage.pds.region
    arns = {joined(p["SourceArn"]) for p in permissions.values()}
    assert {p["SourceAccount"] for p in permissions.values()} == {account}
    assert arns == {f"arn:aws:elasticloadbalancing:{region}:{account}:targetgroup/{n}/*" for n in names}
    for group in groups.values():
        [alias] = [group["Properties"]["Targets"][0]["Id"]["Ref"]]
        mine = [k for k, p in permissions.items() if p["FunctionName"] == {"Ref": alias}]
        assert len(mine) == 1 and mine[0] in group["DependsOn"], (alias, group.get("DependsOn"))


def test_the_policy_function_is_outside_the_vpc_and_reads_only_the_snapshot(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    functions = resources(template, "AWS::Lambda::Function")
    [policy] = [f for f in functions.values() if f["Handler"] == "fast_channels.functions.pds.policy.handler"]

    assert "VpcConfig" not in policy
    assert (policy["Runtime"], policy["Architectures"], policy["MemorySize"], policy["Timeout"]) == (
        "python3.14",
        ["arm64"],
        1769,
        3,
    )
    assert set(policy["Environment"]["Variables"]) >= {"PDS_CONFIG_BUCKET", "FAST_PREFIX"}
    s3 = [s for s in statements(template, "PolicyFunctionRole") if str(s["Action"]).startswith("s3:")]
    assert [s["Action"] for s in s3] == ["s3:GetObject"]
    assert json.dumps(s3[0]["Resource"]).endswith(f'/{SNAPSHOT_KEY}"]]}}')


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


def test_the_own_book_is_warm_and_reads_the_snapshot_and_budgets(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    name, own_book = function(template, OWN_BOOK)

    assert "VpcConfig" not in own_book
    assert (own_book["MemorySize"], own_book["Timeout"]) == (1769, 3)
    assert set(own_book["Environment"]["Variables"]) >= {"PDS_CONFIG_BUCKET", "PDS_TABLE"}
    granted = statements(template, name[:-8] + "Role")
    assert {json.dumps(s["Action"]) for s in granted} >= {'"s3:GetObject"', '"dynamodb:BatchGetItem"'}
    [batch] = [s for s in granted if s["Action"] == "dynamodb:BatchGetItem"]
    assert batch["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["BUDGET#*"]}}
    gate = json.dumps(only(template, "AWS::StepFunctions::StateMachine"))
    assert gate.count("ProvisionedConcurrentExecutions") == 3, "the three functions are warmed"


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


def test_select_reserves_line_ads_and_answers_with_our_tracker(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    name, select = function(template, SELECT)

    assert "VpcConfig" not in select
    assert (select["MemorySize"], select["Timeout"]) == (1769, 3)
    assert set(select["Environment"]["Variables"]) >= {
        "PDS_CONFIG_BUCKET",
        "PDS_TABLE",
        "LEDGER_TABLE",
        "CONTENT_BASE_URL",
    }
    keys = sorted(
        (json.dumps(s["Action"]), s["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"])
        for s in statements(template, name[:-8] + "Role")
        if "Condition" in s
    )
    assert keys == [
        ('"dynamodb:BatchGetItem"', ["BUDGET#*"]),
        ('"dynamodb:PutItem"', ["RES#*"]),
        ('"dynamodb:PutItem"', ["pds/*"]),
        ('"dynamodb:UpdateItem"', ["BUDGET#*", "CAP#*"]),
    ]


def test_the_module_fallback_alarms_watch_prebid_servers_log(deployment: GetDeployment) -> None:
    d = deployment(PDS, extra=EXTRA)
    template = d.template(d.stage.pds)
    filters = {
        f["MetricTransformations"][0]["MetricName"]: f for f in resources(template, "AWS::Logs::MetricFilter").values()
    }

    assert set(filters) == {"Pods", "PolicyFallbacks", "SelectFallbacks", "PodErrors"}
    assert {f["LogGroupName"] for f in filters.values()} == {"fastm-pbs-PrebidContainerLogGroup"}
    assert filters["Pods"]["FilterPattern"] == '"fast-ctv-pod" -"warmup"'
    assert filters["PolicyFallbacks"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "POLICY_"'
    assert filters["SelectFallbacks"]["FilterPattern"] == '"fast-ctv-pod" -"warmup" "SELECT_FALLBACK"'
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
