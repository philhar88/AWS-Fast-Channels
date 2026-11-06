"""The identity module on the playback path (ADR-0018): the edge rebuild on the media distribution, its private access
logs and alarm, the siblings' PRE_SESSION redeem and ticket header, and the sidecar's grants on the ticket rows."""

import json
from collections.abc import Callable
from typing import Any

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.core.monetization.functions import Hook
from fast_channels.core.monetization.playback import HEADER_TICKET, TICKET_TEMPLATE
from fast_channels.core.monetization.validators import validate_bundle
from fast_channels.core.pds.sidecar import sidecar
from fast_channels.infra.constructs.delivery import TRACKER_ROUTE, VIEWER_ACCESS_LOG_FIELDS, path_contract, routes
from fast_channels.infra.constructs.identity.edge import CODE

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
RELEASE = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}
IDENTITY = {**RELEASE, "modules.identity.enabled": True}


def _resources(template: Any, kind: str) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in template.find_resources(kind).items()}


def _behaviours(template: Any) -> dict[str, dict[str, Any]]:
    [distribution] = _resources(template, "AWS::CloudFront::Distribution").values()
    config = distribution["DistributionConfig"]
    return {"*": config["DefaultCacheBehavior"], **{b["PathPattern"]: b for b in config["CacheBehaviors"]}}


def test_the_mediatailor_route_reads_only_behind_the_edge_rebuild(deployment: GetDeployment) -> None:
    d = deployment(IDENTITY)
    media = d.template(d.stage.media)
    functions = _resources(media, "AWS::CloudFront::Function").items()
    [(logical, function)] = [(k, f) for k, f in functions if f["Name"] == "fastm-identity-edge"]
    assert function["AutoPublish"] is True
    assert function["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert function["FunctionCode"] == CODE.read_text()
    behaviours = _behaviours(media)
    mediatailor = behaviours["/v1/*"]
    assert mediatailor["AllowedMethods"] == ["GET", "HEAD", "OPTIONS"], "POST /v1/session gets CloudFront's 403"
    [association] = mediatailor["FunctionAssociations"]
    assert association["EventType"] == "viewer-request"
    assert association["FunctionARN"] == {"Fn::GetAtt": [logical, "FunctionARN"]}
    functions_elsewhere = [p for p, b in behaviours.items() if p != "/v1/*" and b.get("FunctionAssociations")]
    assert functions_elsewhere == [TRACKER_ROUTE.path], "only the demand module's tracker route has its own"


def test_without_the_module_the_route_is_v3s(deployment: GetDeployment) -> None:
    d = deployment(RELEASE)
    media = d.template(d.stage.media)
    names = {f["Name"] for f in _resources(media, "AWS::CloudFront::Function").values()}
    assert "fastm-identity-edge" not in names, "the tracker route's function is the demand module's"
    assert "FunctionAssociations" not in _behaviours(media)["/v1/*"]
    assert len(_behaviours(media)["/v1/*"]["AllowedMethods"]) == 7
    assert routes() == routes(ticketed=False)


def _contract_route(**table: Any) -> dict[str, Any]:
    contract = path_contract(region="us-east-1", media_host="h", **table)
    [route] = [r for r in contract["rules"] if r["path"] == "/v1/*"]
    return dict(route)


def test_the_path_contract_names_the_edge_rebuild() -> None:
    route = _contract_route(table=routes(ticketed=True))
    assert route["methods"] == ["GET", "HEAD", "OPTIONS"] and "ads.t" in route["viewer_request"]
    assert "viewer_request" not in _contract_route()


def test_the_access_logs_leave_out_the_original_query_and_the_viewer(deployment: GetDeployment) -> None:
    on, off = deployment(IDENTITY), deployment(RELEASE)

    def fields(d: Any) -> set[str]:
        deliveries = _resources(d.template(d.stage.media), "AWS::Logs::Delivery").values()
        [cdn] = [p for p in deliveries if "cs-uri-stem" in p.get("RecordFields", [])]
        return set(cdn["RecordFields"])

    assert fields(off) >= VIEWER_ACCESS_LOG_FIELDS
    assert not VIEWER_ACCESS_LOG_FIELDS & fields(on)
    assert fields(off) - fields(on) == VIEWER_ACCESS_LOG_FIELDS


def test_an_edge_error_alarms(deployment: GetDeployment) -> None:
    d = deployment(IDENTITY)
    alarms = {a["AlarmName"]: a for a in _resources(d.template(d.stage.media), "AWS::CloudWatch::Alarm").values()}
    alarm = alarms["fastm-identity-edge-errors"]
    assert (alarm["Namespace"], alarm["MetricName"]) == ("AWS/CloudFront", "FunctionExecutionErrors")
    assert {x["Name"]: x["Value"] for x in alarm["Dimensions"]} == {
        "FunctionName": "fastm-identity-edge",
        "Region": "Global",
    }
    assert (alarm["Threshold"], alarm["ComparisonOperator"], alarm["TreatMissingData"]) == (
        0,
        "GreaterThanThreshold",
        "notBreaching",
    )
    assert alarm["AlarmActions"], "it notifies the alert topic"


def test_the_pair_siblings_redeem_and_forward_the_ticket(deployment: GetDeployment) -> None:
    d = deployment(IDENTITY, extra=EXTRA)
    bundle = d.stage.release.rendered.bundle
    assert validate_bundle(bundle) == []
    ticketed = {
        s.name: HEADER_TICKET in s.playback.ad_decision_server_configuration.http_request.headers
        for s in bundle.siblings
    }
    assert ticketed == {
        "fastm-demo-p-a": True,
        "fastm-demo-p-b": True,
        "fastm-demo-p-z": False,  # the probe
        "fastm-lab-p-a": False,  # the lab channel publishes its hook events
        "fastm-lab-p-b": False,
        "fastm-lab-p-z": False,
    }
    for sibling in bundle.siblings:
        root = bundle.function(sibling.playback.function_mapping[Hook.PRE_SESSION])
        steps = [s.function_id for s in root.steps]
        if ticketed[sibling.name]:
            assert steps == [f"fastm_demo_p_{sibling.letter}_ps_redeem", "fastm_ps_ctl"]
            assert (
                sibling.playback.ad_decision_server_configuration.http_request.headers[HEADER_TICKET] == TICKET_TEMPLATE
            )
        else:
            assert steps == ["fastm_ps_ctl"]
    template = d.template(d.stage.release).to_json()
    ssm = {p["Default"] for p in template["Parameters"].values() if p["Type"].startswith("AWS::SSM::Parameter")}
    assert "/fastm/identity/redeem-url" in ssm, "the redeem URL resolves at deploy"
    [properties] = _resources(d.template(d.stage.release), "Custom::DesiredState").values()
    references = [p["Ref"] for p in properties["Bundle"]["Fn::Join"][1] if isinstance(p, dict)]
    assert any("fastmidentityredeemurl" in r for r in references)


def test_the_ad_server_route_stays_contextual(deployment: GetDeployment) -> None:
    network = {
        **NETWORK,
        "internal_alb_dns": "internal-fastm-pbs-Internal-1234567890.us-east-1.elb.amazonaws.com",
        "internal_alb_zone_id": "Z35SXDOTRQ7X7K",
    }
    d = deployment({**IDENTITY, "modules.demand.ad_server": "mock"}, extra={NETWORK_CONTEXT: network})
    bundle = d.stage.release.rendered.bundle
    assert not any(
        HEADER_TICKET in s.playback.ad_decision_server_configuration.http_request.headers for s in bundle.siblings
    )
    template = d.template(d.stage.release).to_json()
    assert "/fastm/identity/redeem-url" not in json.dumps(template["Parameters"])


def test_the_sidecar_reads_and_binds_ticket_rows_only() -> None:
    arguments: dict[str, Any] = {
        "prefix": "fastm",
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/fastm-pds:x",
        "partition": "aws",
        "region": "us-east-1",
        "account": "123456789012",
        "config_bucket": "fastm-pds-config",
        "content_base_url": "https://d1.cloudfront.net",
        "log_retention_days": 7,
    }
    plain, ticketed = sidecar(**arguments), sidecar(**arguments, identity=True)
    assert "IDENTITY_TABLE" not in plain.environment and ticketed.environment["IDENTITY_TABLE"] == "fastm-identity"
    [grant] = [s for s in ticketed.statements if s["Sid"] == "PdsTicketBindings"]
    assert grant["Action"] == ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    assert grant["Resource"] == "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-identity"
    assert grant["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["T#*"]}}
    assert not [s for s in plain.statements if s["Sid"] == "PdsTicketBindings"]
    [endpoint] = ticketed.dynamodb_endpoint_statements
    assert "arn:aws:dynamodb:us-east-1:123456789012:table/fastm-identity" in endpoint["Resource"]
    [plain_endpoint] = plain.dynamodb_endpoint_statements
    assert not any("identity" in r for r in plain_endpoint["Resource"])
