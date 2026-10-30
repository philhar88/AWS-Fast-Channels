"""The crawl module in the Pds stack: the ``v_crawl_kpis`` view. Its patch contributor and arm run in the PDS's
sidecar, whose settings and grants ``core.pds.sidecar`` renders (``core/tests/pds/test_sidecar.py``)."""

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


def test_off_there_is_no_kpi_view(deployment: Callable[..., Any]) -> None:
    template = pds(deployment, OFF)
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
