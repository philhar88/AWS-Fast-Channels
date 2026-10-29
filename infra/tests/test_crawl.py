"""The crawl module's infrastructure: the catalog-context bucket and its CDN route (Media stack), and the Crawl stack
with the context publisher, its triggers and its grants."""

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk.assertions import Match, Template

from fast_channels.infra.config import FastChannelsConfig
from fast_channels.infra.constructs.delivery import (
    CACHING_OPTIMIZED_UNCOMPRESSED,
    CATALOG_CONTEXT_PATH,
    DEFAULT_PATH,
    ROUTES,
    Origin,
    path_contract,
    routes,
)
from fast_channels.infra.stacks.crawl import CrawlStack

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
"""Every module on, as ``STAGE=fastm`` deploys them."""
CRAWL_OFF: dict[str, Any] = {**FASTM, "modules": {**FASTM["modules"], "crawl": {"enabled": False}}}


@pytest.fixture(scope="module")
def crawl(deployment: Callable[..., Any]) -> Template:
    stack = deployment(FASTM).stage.crawl
    assert stack is not None and stack.stack_name == "FastChannels-fastm-Crawl"
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def media(deployment: Callable[..., Any]) -> Template:
    return Template.from_stack(deployment(FASTM).stage.media)


def _statements(template: Template, kind: str = "AWS::IAM::Policy") -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources(kind).values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _distribution(media: Template) -> dict[str, Any]:
    [distribution] = media.find_resources("AWS::CloudFront::Distribution").values()
    config: dict[str, Any] = distribution["Properties"]["DistributionConfig"]
    return config


def _catalog_bucket(media: Template) -> str:
    [logical_id] = [key for key in media.find_resources("AWS::S3::Bucket") if key.startswith("CatalogContextsBucket")]
    return logical_id


# --- the catalog-context bucket and its route (Media stack) ---------------------------------------------------------


def test_the_bucket_and_the_route_exist_only_with_the_module(deployment: Callable[..., Any]) -> None:
    off = deployment(CRAWL_OFF).stage
    assert off.crawl is None and off.media.catalog_contexts is None
    assert CATALOG_CONTEXT_PATH not in json.dumps(Template.from_stack(off.media).to_json())
    assert routes() == ROUTES  # the v3.0.0 table, and docs/path-contract.json with it


def test_the_bucket_is_private_encrypted_and_tls_only(media: Template) -> None:
    bucket = media.find_resources("AWS::S3::Bucket")[_catalog_bucket(media)]["Properties"]
    assert bucket["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert bucket["BucketEncryption"]["ServerSideEncryptionConfiguration"] == [
        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    ]
    assert bucket["OwnershipControls"] == {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    statements = _statements(media, "AWS::S3::BucketPolicy")
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"Bool": {"aws:SecureTransport": "false"}} for s in statements
    )
    assert any(
        s["Effect"] == "Deny" and s["Condition"] == {"NumericLessThan": {"s3:TlsVersion": 1.2}} for s in statements
    )


def test_only_this_distribution_reads_the_contexts_and_lists_the_bucket(media: Template) -> None:
    bucket = _catalog_bucket(media)
    [distribution] = media.find_resources("AWS::CloudFront::Distribution")
    allows = [s for s in _statements(media, "AWS::S3::BucketPolicy") if s["Effect"] == "Allow"]
    source = {
        "StringEquals": {
            "AWS:SourceArn": {
                "Fn::Join": [
                    "",
                    ["arn:aws:cloudfront::123456789012:distribution/", {"Ref": distribution}],
                ]
            }
        }
    }
    reads = [s for s in allows if s["Action"] == "s3:GetObject" and bucket in json.dumps(s["Resource"])]
    lists = [s for s in allows if s["Action"] == "s3:ListBucket" and bucket in json.dumps(s["Resource"])]
    assert [(s["Principal"], s["Condition"]) for s in reads + lists] == [
        ({"Service": "cloudfront.amazonaws.com"}, source)
    ] * 2
    assert reads[0]["Resource"] == {"Fn::Join": ["", [{"Fn::GetAtt": [bucket, "Arn"]}, "/catalog/ctx/*"]]}
    assert lists[0]["Resource"] == {"Fn::GetAtt": [bucket, "Arn"]}
    others = [s for s in allows if bucket in json.dumps(s["Resource"]) and s not in reads + lists]
    assert len(others) == 1 and "s3:DeleteObject*" in others[0]["Action"]  # CDK's auto-delete at stack deletion


def test_the_route_reaches_only_objects_under_catalog_ctx(media: Template) -> None:
    """The behaviour can never ask the bucket for its root or a listing: its path pattern pins every forwarded path
    under /catalog/ctx/, the origin adds no path, and no query string, header or cookie reaches S3 (the cache key
    has none and there is no origin request policy). S3 thus only ever sees GetObject or HeadObject of a key."""
    config = _distribution(media)
    [behaviour] = [b for b in config["CacheBehaviors"] if b["PathPattern"] == CATALOG_CONTEXT_PATH]
    assert behaviour["TargetOriginId"] == Origin.CATALOG.value
    assert behaviour["CachePolicyId"] == CACHING_OPTIMIZED_UNCOMPRESSED.policy.cache_policy_id
    assert CACHING_OPTIMIZED_UNCOMPRESSED.contract()["key"] == {
        "query_strings": "none",
        "headers": [],
        "cookies": "none",
    }
    assert "OriginRequestPolicyId" not in behaviour
    assert behaviour["AllowedMethods"] == ["GET", "HEAD"] and behaviour["CachedMethods"] == ["GET", "HEAD"]
    assert behaviour["ViewerProtocolPolicy"] == "https-only"
    assert CATALOG_CONTEXT_PATH.startswith("/catalog/ctx/") and CATALOG_CONTEXT_PATH.endswith("/*")
    [origin] = [o for o in config["Origins"] if o["Id"] == Origin.CATALOG.value]
    assert origin.get("OriginPath", "") == ""
    assert "OriginAccessControlId" in origin and origin["S3OriginConfig"] == {"OriginAccessIdentity": ""}
    assert _catalog_bucket(media) in json.dumps(origin["DomainName"])
    paths = [b["PathPattern"] for b in config["CacheBehaviors"]]
    assert paths == ["/tm/*", "/v1/*", CATALOG_CONTEXT_PATH]  # ahead of the default route (the media bucket)


def test_the_path_contract_names_the_route_and_its_origin() -> None:
    contract = path_contract(region="us-east-1", media_host="d1.cloudfront.net", table=routes(catalog_context=True))
    assert [rule["path"] for rule in contract["rules"]] == ["/tm/*", "/v1/*", CATALOG_CONTEXT_PATH, DEFAULT_PATH]
    assert contract["origins"][Origin.CATALOG]["host"] == "d1.cloudfront.net"
    rule = contract["rules"][2]
    assert rule["methods"] == ["GET", "HEAD"] and "forward" not in rule
    assert Origin.CATALOG not in path_contract(region="us-east-1", media_host="d1.cloudfront.net")["origins"]


def test_the_module_changes_nothing_else_in_the_media_stack(deployment: Callable[..., Any], normalized: Any) -> None:
    """Crawl on adds the bucket, its policy and auto-delete, the origin with its access control, the behaviour and the
    bucket's outputs to the Media stack, and changes nothing else."""
    on = normalized(Template.from_stack(deployment(FASTM).stage.media))
    off = normalized(Template.from_stack(deployment(CRAWL_OFF).stage.media))
    added = sorted(set(on["Resources"]) - set(off["Resources"]))
    assert [key[:-8] for key in added] == [  # logical ids end in an 8-character hash
        "CatalogContextsBucketAutoDeleteObjectsCustomResource",
        "CatalogContextsBucket",
        "CatalogContextsBucketPolicy",
        "CustomS3AutoDeleteObjectsCustomResourceProviderHandler",
        "CustomS3AutoDeleteObjectsCustomResourceProviderHandlerLogs",
        "CustomS3AutoDeleteObjectsCustomResourceProviderRole",
        "DeliveryDistributionOrigin4S3OriginAccessControl",
    ], added
    assert sorted(set(on["Outputs"]) - set(off["Outputs"])) == [
        "PublishOutputFnGetAttCatalogContextsBucketE67C84ADArnFB352F9B",
        "PublishOutputRefCatalogContextsBucketE67C84ADBF7FF690",
    ]
    assert set(off["Resources"]) <= set(on["Resources"])
    changed = sorted(k for k in off["Resources"] if off["Resources"][k] != on["Resources"][k])
    assert [k for k in changed if not k.startswith("DeliveryDistribution")] == [], changed
    config_on = on["Resources"][changed[0]]["Properties"]["DistributionConfig"]
    config_off = off["Resources"][changed[0]]["Properties"]["DistributionConfig"]
    assert {k for k in config_on if config_on[k] != config_off.get(k)} == {"CacheBehaviors", "Origins"}


# --- the Crawl stack ------------------------------------------------------------------------------------------------


def test_the_stack_exists_only_with_the_module(default: Any, deployment: Callable[..., Any]) -> None:
    assert default.stage.crawl is None
    assert deployment(CRAWL_OFF).stage.crawl is None


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
                        "CATALOG_CONTEXT_BUCKET": Match.any_value(),
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
    assert "PUBLISHER_DOMAIN" not in variables and "MEDIA_BUCKET" not in variables
    crawl_on = {**FASTM["modules"]["crawl"], "publisher_domain": "example.com"}
    stage = deployment({**FASTM, "modules": {**FASTM["modules"], "crawl": crawl_on}})
    Template.from_stack(stage.stage.crawl).has_resource_properties(
        "AWS::Lambda::Function",
        {"Environment": {"Variables": Match.object_like({"PUBLISHER_DOMAIN": "example.com"})}},
    )


def test_bucket_access_is_the_context_prefix_of_the_catalog_bucket_only(crawl: Template) -> None:
    s3 = [s for s in _statements(crawl) if any(str(a).startswith("s3:") for a in _list(s["Action"]))]
    objects = next(s for s in s3 if "s3:PutObject" in _list(s["Action"]))
    assert sorted(_list(objects["Action"])) == ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    assert json.dumps(objects["Resource"]).endswith('"/catalog/ctx/v1/*"]]}')
    listing = next(s for s in s3 if _list(s["Action"]) == ["s3:ListBucket"])
    assert listing["Condition"] == {"StringLike": {"s3:prefix": ["catalog/ctx/v1/*"]}}
    assert len(s3) == 2  # nothing else, and nothing on any other prefix or bucket
    assert all("CatalogContexts" in json.dumps(s["Resource"]) for s in s3)


def test_nothing_in_the_stack_touches_the_ingest_or_the_media_bucket(crawl: Template) -> None:
    text = json.dumps(crawl.to_json())
    assert "StorageIngest" not in text and "StorageMedia" not in text


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
    assert set(outputs) == {"CatalogContextBaseUrl", "CatalogContextPublisherName", "CatalogContextBucketName"}


def test_template_snapshot(crawl: Template, normalized: Any, snapshot_json: SnapshotAssertion) -> None:
    assert normalized(crawl) == snapshot_json


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_the_crawl_stack_needs_the_media_stacks_bucket(fresh_deployment: Callable[..., Any]) -> None:
    deployment = fresh_deployment(FASTM)
    stage = deployment.stage
    config = FastChannelsConfig.from_context(deployment.app.node)
    without = dataclasses.replace(stage.media.exports, catalog_context_bucket=None)
    with pytest.raises(ValueError, match="catalog-context bucket"):
        CrawlStack(stage, "Again", config=config, data=stage.data.exports, media=without)
