"""The crawl module in the Pds stack: the policy function's patch contributor (its settings and its read-only access
to the catalog-context bucket), the arm for ``/select``'s trackers, and the ``v_crawl_kpis`` view."""

import base64
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from aws_cdk.assertions import Template

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.infra.constructs.crawl.kpis import KPIS

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
OFF: dict[str, Any] = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}
ON: dict[str, Any] = OFF | {"modules.crawl.enabled": True, "modules.crawl.publisher_domain": "example.com"}


def pds(deployment: Callable[..., Any], overrides: dict[str, Any]) -> Template:
    d = deployment(overrides, extra=EXTRA)
    return Template.from_stack(d.stage.pds)


def function(template: Template, handler: str) -> dict[str, Any]:
    [properties] = [
        r["Properties"]
        for r in template.find_resources("AWS::Lambda::Function").values()
        if r["Properties"].get("Handler") == handler
    ]
    result: dict[str, Any] = properties
    return result


def statements_of(template: Template, handler: str) -> list[dict[str, Any]]:
    """The statements of the policies on the role of ``handler``'s function."""
    role = function(template, handler)["Role"]["Fn::GetAtt"][0]
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        if {"Ref": role} in policy["Properties"]["Roles"]
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


POLICY = "fast_channels.functions.pds.policy.handler"
SELECT = "fast_channels.functions.pds.select.handler"


def test_the_policy_function_gets_the_contributors_settings(deployment: Callable[..., Any]) -> None:
    template = pds(deployment, ON)
    variables = function(template, POLICY)["Environment"]["Variables"]
    assert (variables["CRAWL_HOLDOUT_PERCENT"], variables["PBS_VERSION"], variables["PUBLISHER_DOMAIN"]) == (
        "20",
        "3.43.0",
        "example.com",
    )
    [parameter] = [
        name
        for name, p in template.to_json()["Parameters"].items()
        if p.get("Default") == "/fastm/pds/catalog-context-bucket"
    ]
    assert variables["CATALOG_CONTEXT_BUCKET"] == {"Ref": parameter}
    select = function(template, SELECT)["Environment"]["Variables"]
    assert select["CRAWL_HOLDOUT_PERCENT"] == "20"
    assert not {"CATALOG_CONTEXT_BUCKET", "PBS_VERSION"} & select.keys()


def test_the_release_follows_the_demand_modules(deployment: Callable[..., Any]) -> None:
    template = pds(deployment, ON | {"modules.demand.pbs_version": "4.4.0", "modules.crawl.holdout_percent": 30})
    variables = function(template, POLICY)["Environment"]["Variables"]
    assert (variables["PBS_VERSION"], variables["CRAWL_HOLDOUT_PERCENT"]) == ("4.4.0", "30")


def test_the_policy_function_only_lists_and_reads_the_context_objects(deployment: Callable[..., Any]) -> None:
    template = pds(deployment, ON)
    bucket = function(template, POLICY)["Environment"]["Variables"]["CATALOG_CONTEXT_BUCKET"]
    touching = [s for s in statements_of(template, POLICY) if bucket["Ref"] in json.dumps(s["Resource"])]
    assert sorted((s["Action"], s.get("Condition")) for s in touching) == [
        ("s3:GetObject", None),
        ("s3:ListBucket", {"StringEquals": {"s3:prefix": "catalog/ctx/v1/"}}),
    ]
    [read] = [s for s in touching if s["Action"] == "s3:GetObject"]
    [objects] = [r for r in read["Resource"] if bucket["Ref"] in json.dumps(r)]
    assert objects["Fn::Join"][1][-1] == "/catalog/ctx/v1/*"


def test_off_the_functions_and_the_records_are_as_before(deployment: Callable[..., Any]) -> None:
    template = pds(deployment, OFF)
    for handler in (POLICY, SELECT):
        variables = function(template, handler)["Environment"]["Variables"]
        assert not {"CRAWL_HOLDOUT_PERCENT", "CATALOG_CONTEXT_BUCKET", "PBS_VERSION"} & variables.keys()
    names = [t["Properties"]["TableInput"]["Name"] for t in template.find_resources("AWS::Glue::Table").values()]
    assert "v_crawl_kpis" not in names


def _view(template: Template) -> dict[str, Any]:
    [view] = [
        t["Properties"]["TableInput"]
        for t in template.find_resources("AWS::Glue::Table").values()
        if t["Properties"]["TableInput"]["Name"] == "v_crawl_kpis"
    ]
    return view


def test_the_kpi_view_reads_the_three_decision_tables(deployment: Callable[..., Any]) -> None:
    view = _view(pds(deployment, ON))
    assert view["TableType"] == "VIRTUAL_VIEW"
    encoded = re.fullmatch(r"/\* Presto View: (.+) \*/", view["ViewOriginalText"])
    assert encoded is not None
    definition = json.loads(base64.b64decode(encoded[1]))
    assert definition["schema"] == "fastm_lake"
    assert [c["name"] for c in definition["columns"]] == [name for name, _ in KPIS.columns]
    sql = definition["originalSql"]
    for table in ("pds_policy", "pds_select", "pds_delivery"):
        assert f'"fastm_lake"."{table}"' in sql
    assert "contains(contributors, 'crawl')" in sql and "NOT probe" in sql
    assert "json_extract_scalar(contributed, '$.enr')" in sql and "json_extract_scalar(contributed, '$.ctx')" in sql


@pytest.mark.parametrize(("name", "kind"), [("coverage", "double"), ("enr", "bigint"), ("datehour", "varchar")])
def test_the_kpi_columns(name: str, kind: str) -> None:
    assert dict(KPIS.columns)[name] == kind
