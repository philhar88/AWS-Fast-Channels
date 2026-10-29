"""The Crawl stack (``modules.crawl``): the catalog context publisher, its triggers and its grants."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk.assertions import Match, Template

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
"""Every module on, as ``STAGE=fastm`` deploys them."""


@pytest.fixture(scope="module")
def crawl(deployment: Callable[..., Any]) -> Template:
    stack = deployment(FASTM).stage.crawl
    assert stack is not None and stack.stack_name == "FastChannels-fastm-Crawl"
    return Template.from_stack(stack)


def _statements(template: Template) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def test_the_stack_exists_only_with_the_module(default: Any, deployment: Callable[..., Any]) -> None:
    assert default.stage.crawl is None
    off = {**FASTM, "modules": {**FASTM["modules"], "crawl": {"enabled": False}}}
    assert deployment(off).stage.crawl is None


def test_the_publisher_function(crawl: Template, deployment: Callable[..., Any]) -> None:
    crawl.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "fast_channels.functions.crawl.publisher.handler",
            "Timeout": 300,
            "Environment": {
                "Variables": Match.object_like(
                    {
                        "FAST_PREFIX": "fastm",
                        "CATALOG_TABLE": Match.any_value(),
                        "LIBRARY_TABLE": Match.any_value(),
                        "MEDIA_BUCKET": Match.any_value(),
                    }
                )
            },
        },
    )
    [variables] = [
        fn["Properties"]["Environment"]["Variables"]
        for fn in crawl.find_resources("AWS::Lambda::Function").values()
        if fn["Properties"].get("Handler", "").endswith("crawl.publisher.handler")
    ]
    assert "PUBLISHER_DOMAIN" not in variables  # not configured on fastm
    crawl_on = {**FASTM["modules"]["crawl"], "publisher_domain": "example.com"}
    stage = deployment({**FASTM, "modules": {**FASTM["modules"], "crawl": crawl_on}})
    Template.from_stack(stage.stage.crawl).has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"PUBLISHER_DOMAIN": "example.com"})}},
    )


def test_bucket_access_is_the_context_prefix_only(crawl: Template) -> None:
    s3 = [s for s in _statements(crawl) if any(str(a).startswith("s3:") for a in _list(s["Action"]))]
    objects = next(s for s in s3 if "s3:PutObject" in _list(s["Action"]))
    assert sorted(_list(objects["Action"])) == ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    assert json.dumps(objects["Resource"]).endswith('"/catalog/ctx/v1/*"]]}')
    listing = next(s for s in s3 if _list(s["Action"]) == ["s3:ListBucket"])
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["catalog/ctx/v1/*"]}}
    assert len(s3) == 2  # nothing else, and nothing on any other prefix or bucket


def test_nothing_in_the_stack_touches_the_ingest_bucket(crawl: Template) -> None:
    assert "IngestBucket" not in json.dumps(crawl.to_json())
    assert not any(
        "Ingest" in json.dumps(s.get("Resource")) for s in _statements(crawl) if "s3:PutObject" in _list(s["Action"])
    )


def test_tables_are_read_only(crawl: Template) -> None:
    dynamo = [s for s in _statements(crawl) if any(str(a).startswith("dynamodb:") for a in _list(s["Action"]))]
    assert [sorted(_list(s["Action"])) for s in dynamo] == [["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]]


def test_triggers(crawl: Template) -> None:
    crawl.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["fast-channels.fastm"],
                "detail-type": ["Schedule Published", "Catalog Item Changed"],
            },
            "Targets": [
                Match.object_like({"RetryPolicy": {"MaximumRetryAttempts": 8, "MaximumEventAgeInSeconds": 21600}})
            ],
        },
    )
    crawl.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(1 day)",
            "Target": Match.object_like({"Input": '{"sweep":true}'}),
        },
    )
    crawl.has_resource_properties("AWS::Scheduler::ScheduleGroup", {"Name": "fastm-crawl"})
    crawl.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {"AlarmName": "fastm-crawl-publisher-dead-letters", "AlarmActions": [Match.any_value()]},
    )


def test_outputs(crawl: Template) -> None:
    outputs = crawl.find_outputs("*")
    assert set(outputs) == {"CatalogContextBaseUrl", "CatalogContextPublisherName"}


def test_template_snapshot(crawl: Template, normalized: Any, snapshot_json: SnapshotAssertion) -> None:
    assert normalized(crawl) == snapshot_json


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
