"""MediaDistribution, CdnAccessLogs and EdgeStack in isolated stacks, plus the generated path contract."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack, Validations
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Annotations, Match, Template
from cdk_nag import AwsSolutionsChecks, ServerlessChecks

from fast_channels.infra.config import FastChannelsConfig
from fast_channels.infra.constructs.delivery import (
    ACCESS_LOG_FIELDS,
    DEFAULT_PATH,
    ROUTES,
    CdnAccessLogs,
    MediaDistribution,
    Origin,
    path_contract,
)
from fast_channels.infra.stacks.data import DataStack
from fast_channels.infra.stacks.edge import EDGE_REGION, EdgeStack

ACCOUNT = "123456789012"
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT: dict[str, Any] = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
DOCUMENTED_CONTRACT = REPO_ROOT / "docs" / "path-contract.json"
DOCUMENTED_HOSTS = {"region": "<region>", "media_host": "<distribution>.cloudfront.net"}
CERTIFICATE = f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/abc"

# MediaTailor's production CloudFront configuration, in precedence order: path -> (origin, cache, origin request,
# headers policy). SSAI only: no SGAI routes (/v1/i-media/*, /preconditioned-ads/*). /v1/* forwards every viewer
# header (AllViewerAndCloudFrontHeaders-2022-06): MediaTailor requires Accept-Encoding, User-Agent, Host and
# X-Forwarded-For.
CUSTOM = "custom"
EXPECTED_BEHAVIOURS = [
    ("/tm/*", "mediatailor-segments", "b2884449-e4de-46a7-ac36-70bc7f1ddd6d", None, CUSTOM),
    (
        "/v1/*",
        "mediatailor-manifests",
        "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        "33f36d7e-f396-46d9-90e0-52428a34d9dc",
        "eaab4381-ed33-4a86-88ca-d9558dc6cd63",
    ),
    ("*", "media", "b2884449-e4de-46a7-ac36-70bc7f1ddd6d", "88a5eaf4-2fd4-4709-b370-b4c650ea3fcf", CUSTOM),
]
READ = ["GET", "HEAD", "OPTIONS"]


def new_stack(region: str = "us-east-1", *, outdir: Path | None = None) -> Stack:
    app = App(context=CONTEXT, outdir=str(outdir) if outdir else None)
    return Stack(app, "Test", env=Environment(account=ACCOUNT, region=region))


def imported_bucket(stack: Stack, name: str) -> s3.IBucket:
    """A bucket as consumer stacks see it (DataExports): imported, its policy owned elsewhere."""
    return s3.Bucket.from_bucket_attributes(stack, name, bucket_name=name, bucket_arn=f"arn:aws:s3:::{name}")


def media_distribution(stack: Stack, **props: Any) -> MediaDistribution:
    return MediaDistribution(stack, "Delivery", media_bucket=imported_bucket(stack, "media"), **props)


def only(template: Template, type_: str) -> Mapping[str, Any]:
    [resource] = template.find_resources(type_).values()
    return resource


def distribution_config(template: Template) -> Mapping[str, Any]:
    config: Mapping[str, Any] = only(template, "AWS::CloudFront::Distribution")["Properties"]["DistributionConfig"]
    return config


@pytest.fixture(scope="module")
def delivery() -> tuple[MediaDistribution, Template]:
    stack = new_stack()
    construct = media_distribution(stack)
    return construct, Template.from_stack(stack)


@pytest.fixture(scope="module")
def template(delivery: tuple[MediaDistribution, Template]) -> Template:
    return delivery[1]


def test_behaviours_route_in_the_verified_order_with_the_managed_policies(template: Template) -> None:
    config = distribution_config(template)
    custom = {"Ref": next(iter(template.find_resources("AWS::CloudFront::ResponseHeadersPolicy")))}
    behaviours = [*config["CacheBehaviors"], {**config["DefaultCacheBehavior"], "PathPattern": "*"}]

    assert [
        (
            b["PathPattern"],
            b["TargetOriginId"],
            b["CachePolicyId"],
            b.get("OriginRequestPolicyId"),
            CUSTOM if b["ResponseHeadersPolicyId"] == custom else b["ResponseHeadersPolicyId"],
        )
        for b in behaviours
    ] == EXPECTED_BEHAVIOURS


def test_behaviour_methods_compression_and_viewer_protocol(template: Template) -> None:
    config = distribution_config(template)
    behaviours = {b["PathPattern"]: b for b in config["CacheBehaviors"]} | {"*": config["DefaultCacheBehavior"]}

    assert {path for path, b in behaviours.items() if b["Compress"]} == {"/v1/*"}
    assert behaviours["/v1/*"]["AllowedMethods"] == ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    assert behaviours["/v1/*"]["CachedMethods"] == ["GET", "HEAD"]
    assert all(b["AllowedMethods"] == READ == b["CachedMethods"] for p, b in behaviours.items() if p != "/v1/*")
    assert {b["ViewerProtocolPolicy"] for b in behaviours.values()} == {"https-only"}


def test_distribution_serves_http3_and_ipv6_on_the_cloudfront_certificate(template: Template) -> None:
    config = distribution_config(template)

    assert (config["HttpVersion"], config["IPV6Enabled"], config["PriceClass"]) == ("http2and3", True, "PriceClass_All")
    assert "Aliases" not in config
    assert "ViewerCertificate" not in config
    assert "Logging" not in config  # standard logging v2 is CdnAccessLogs


def test_mediatailor_origins_are_regional_https_endpoints(template: Template) -> None:
    origins = {o["Id"]: o for o in distribution_config(template)["Origins"]}

    for origin, host in [
        (Origin.MANIFESTS, "manifests.mediatailor.us-east-1.amazonaws.com"),
        (Origin.SEGMENTS, "segments.mediatailor.us-east-1.amazonaws.com"),
    ]:
        assert origins[origin]["DomainName"] == host
        assert origins[origin]["CustomOriginConfig"] == {
            "OriginProtocolPolicy": "https-only",
            "OriginSSLProtocols": ["TLSv1.2"],
            "OriginReadTimeout": 10,
            "OriginKeepaliveTimeout": 5,
        }
        assert (origins[origin]["ConnectionAttempts"], origins[origin]["ConnectionTimeout"]) == (3, 10)
        assert "OriginAccessControlId" not in origins[origin]


def test_media_bucket_is_read_through_origin_access_control_without_a_second_bucket_policy(
    delivery: tuple[MediaDistribution, Template],
) -> None:
    construct, template = delivery
    media = {o["Id"]: o for o in distribution_config(template)["Origins"]}[Origin.MEDIA]
    oac = only(template, "AWS::CloudFront::OriginAccessControl")["Properties"]["OriginAccessControlConfig"]

    assert media["DomainName"] == "media.s3.us-east-1.amazonaws.com"
    assert media["OriginAccessControlId"]["Fn::GetAtt"][1] == "Id"
    assert (oac["OriginAccessControlOriginType"], oac["SigningBehavior"], oac["SigningProtocol"]) == (
        "s3",
        "always",
        "sigv4",
    )
    template.resource_count_is("AWS::S3::BucketPolicy", 0)
    assert Annotations.from_stack(Stack.of(construct)).find_warning("*", Match.any_value()) == []


def test_mediatailor_origins_have_no_origin_access_control(template: Template) -> None:
    origins = {o["Id"]: o for o in distribution_config(template)["Origins"]}

    assert "OriginAccessControlId" not in origins[Origin.MANIFESTS]
    assert "OriginAccessControlId" not in origins[Origin.SEGMENTS]


def test_one_custom_response_headers_policy_and_no_custom_cache_policy(template: Template) -> None:
    template.resource_count_is("AWS::CloudFront::CachePolicy", 0)
    template.resource_count_is("AWS::CloudFront::OriginRequestPolicy", 0)
    config = only(template, "AWS::CloudFront::ResponseHeadersPolicy")["Properties"]["ResponseHeadersPolicyConfig"]

    assert config["CorsConfig"] == {
        "AccessControlAllowCredentials": False,
        "AccessControlAllowHeaders": {"Items": ["*"]},
        "AccessControlAllowMethods": {"Items": READ},
        "AccessControlAllowOrigins": {"Items": ["*"]},
        "AccessControlExposeHeaders": {"Items": ["*"]},
        "AccessControlMaxAgeSec": 86400,
        "OriginOverride": True,
    }
    assert config["CustomHeadersConfig"] == {
        "Items": [{"Header": "Timing-Allow-Origin", "Override": True, "Value": "*"}]
    }
    assert config["ServerTimingHeadersConfig"] == {"Enabled": True, "SamplingRate": 100}


def test_custom_content_domain_with_a_modern_tls_policy() -> None:
    stack = new_stack()
    construct = media_distribution(
        stack,
        content_domain="media.example.com",
        certificate_arn=CERTIFICATE,
        price_class=cf.PriceClass.PRICE_CLASS_100,
    )
    config = distribution_config(Template.from_stack(stack))

    assert config["Aliases"] == ["media.example.com"]
    assert config["ViewerCertificate"] == {
        "AcmCertificateArn": CERTIFICATE,
        "MinimumProtocolVersion": "TLSv1.2_2025",
        "SslSupportMethod": "sni-only",
    }
    assert config["PriceClass"] == "PriceClass_100"
    assert construct.content_base_url == construct.cdn_base_url == "https://media.example.com"


def test_content_domain_requires_a_certificate() -> None:
    with pytest.raises(ValueError, match="must be given together"):
        media_distribution(new_stack(), content_domain="media.example.com")


def test_base_urls_default_to_the_cloudfront_domain(delivery: tuple[MediaDistribution, Template]) -> None:
    construct, _ = delivery
    stack = Stack.of(construct)
    domain = stack.resolve(construct.distribution.distribution_domain_name)

    assert stack.resolve(construct.content_base_url) == {"Fn::Join": ["", ["https://", domain]]}
    assert construct.cdn_base_url == construct.content_base_url


def test_the_construct_creates_no_stack_outputs(delivery: tuple[MediaDistribution, Template]) -> None:
    construct, template = delivery
    contract = Stack.of(construct).resolve(construct.path_contract)
    distribution = Stack.of(construct).resolve(construct.distribution.distribution_domain_name)

    assert "Outputs" not in template.to_json()  # MediaStack outputs CdnBaseUrl, DistributionId and PathContract
    assert contract["origins"]["media"]["host"] == distribution
    assert contract["origins"]["mediatailor-manifests"]["host"] == "manifests.mediatailor.us-east-1.amazonaws.com"


def test_two_distributions_fit_in_one_stack() -> None:
    stack = new_stack()
    bucket = imported_bucket(stack, "media")
    for id in ("One", "Two"):
        MediaDistribution(stack, id, media_bucket=bucket)

    Template.from_stack(stack).resource_count_is("AWS::CloudFront::Distribution", 2)


def test_path_contract_mirrors_the_routes() -> None:
    contract = path_contract(region="eu-west-1", media_host="d123.cloudfront.net")

    assert [rule["path"] for rule in contract["rules"]] == [route.path for route in ROUTES]
    assert ROUTES[-1].path == DEFAULT_PATH
    assert {name: origin["host"] for name, origin in contract["origins"].items()} == {
        "media": "d123.cloudfront.net",
        "mediatailor-manifests": "manifests.mediatailor.eu-west-1.amazonaws.com",
        "mediatailor-segments": "segments.mediatailor.eu-west-1.amazonaws.com",
    }
    personalised = contract["rules"][1]
    assert personalised["path"] == "/v1/*"
    assert personalised["methods"] == ["GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"]
    # Every viewer header: MediaTailor requires Accept-Encoding, User-Agent, Host and X-Forwarded-For.
    assert personalised["forward"] == {
        "headers": ["*"],
        "query_strings": "all",
        "cookies": "all",
        "cloudfront_policy": {
            "id": "33f36d7e-f396-46d9-90e0-52428a34d9dc",
            "name": "Managed-AllViewerAndCloudFrontHeaders-2022-06",
        },
    }
    assert personalised["cache"]["cloudfront_policy"]["name"] == "Managed-CachingDisabled"
    assert "forward" not in contract["rules"][0]  # only the cache key reaches the origin
    assert not {"/v1/i-media/*", "/preconditioned-ads/*"} & {rule["path"] for rule in contract["rules"]}  # SGAI
    assert contract["rules"][-1]["cache"]["key"] == {"query_strings": "none", "headers": [], "cookies": "none"}


def test_documented_path_contract_is_current(request: pytest.FixtureRequest) -> None:
    """``docs/path-contract.json`` is generated: ``pytest infra/tests/test_delivery.py --snapshot-update``."""
    expected = json.dumps(path_contract(**DOCUMENTED_HOSTS), indent=2) + "\n"
    if request.config.getoption("--snapshot-update"):
        DOCUMENTED_CONTRACT.write_text(expected)

    assert DOCUMENTED_CONTRACT.read_text() == expected, "regenerate with --snapshot-update"


@pytest.fixture(scope="module")
def access_logs() -> tuple[str, Template]:
    """Logical id of the distribution, and a template with its access logs."""
    stack = new_stack()
    construct = media_distribution(stack)
    CdnAccessLogs(
        stack,
        "AccessLogs",
        distribution=construct.distribution,
        logs_bucket=imported_bucket(stack, "logs"),
        name="fast",
    )
    return stack.resolve(construct.distribution.distribution_id)["Ref"], Template.from_stack(stack)


def test_access_logs_go_to_s3_as_hive_partitioned_parquet(access_logs: tuple[str, Template]) -> None:
    distribution, template = access_logs
    source = only(template, "AWS::Logs::DeliverySource")["Properties"]
    destinations = {
        d["Properties"]["Name"]: d["Properties"]
        for d in template.find_resources("AWS::Logs::DeliveryDestination").values()
    }
    deliveries = template.find_resources("AWS::Logs::Delivery")
    [to_s3] = [d for d in deliveries.values() if "S3EnableHiveCompatiblePath" in d["Properties"]]

    assert source == {
        "Name": "fast-cdn-access",
        "LogType": "ACCESS_LOGS",
        "ResourceArn": {"Fn::Join": ["", [f"arn:aws:cloudfront::{ACCOUNT}:distribution/", {"Ref": distribution}]]},
    }
    assert destinations["fast-cdn-access-s3"] == {
        "Name": "fast-cdn-access-s3",
        "DestinationResourceArn": "arn:aws:s3:::logs",
        "OutputFormat": "parquet",
    }
    assert to_s3["Properties"]["S3EnableHiveCompatiblePath"] is True
    assert to_s3["Properties"]["S3SuffixPath"] == "{DistributionId}/{yyyy}/{MM}/{dd}/{HH}"
    assert to_s3["Properties"]["RecordFields"] == list(ACCESS_LOG_FIELDS)
    assert {"x-edge-request-id", "cmcd-session-id", "cmcd-content-id", "cs-uri-query"} <= set(ACCESS_LOG_FIELDS)
    assert not {"cs(Cookie)", "fle-status"} & set(ACCESS_LOG_FIELDS)


def test_access_logs_stay_in_s3(access_logs: tuple[str, Template]) -> None:
    _, template = access_logs

    template.resource_count_is("AWS::Logs::LogGroup", 0)
    template.resource_count_is("AWS::Logs::Delivery", 1)
    template.resource_count_is("AWS::Logs::ResourcePolicy", 0)


def test_access_logs_need_a_us_east_1_stack() -> None:
    stack = new_stack("eu-west-1")
    construct = media_distribution(stack)
    with pytest.raises(ValueError, match="must be in a us-east-1 stack"):
        CdnAccessLogs(
            stack, "Logs", distribution=construct.distribution, logs_bucket=imported_bucket(stack, "logs"), name="x"
        )


def edge_deployment(outdir: Path | None = None) -> EdgeStack:
    """Data and a distribution in eu-west-1, with the Edge stack in us-east-1."""
    app = App(context=CONTEXT, outdir=str(outdir) if outdir else None)
    config = FastChannelsConfig.model_validate(
        {"agent": {"enabled": False}, "notifications": {"email": "ops@example.com"}}, context={"region": "eu-west-1"}
    )
    elsewhere = Environment(account=ACCOUNT, region="eu-west-1")
    data = DataStack(app, "Data", config=config, env=elsewhere)
    media = Stack(app, "Media", env=elsewhere)
    distribution = MediaDistribution(media, "Delivery", media_bucket=data.exports.media_bucket).distribution
    return EdgeStack(
        app,
        "Edge",
        config=config,
        data=data.exports,
        distribution=distribution,
        env=Environment(account=ACCOUNT, region=EDGE_REGION),
    )


def test_edge_stack_logs_and_alarms_a_distribution_of_another_region() -> None:
    template = Template.from_stack(edge_deployment())

    def output_of(stack: str) -> dict[str, Any]:
        return {"Fn::GetStackOutput": {"StackName": stack, "Region": "eu-west-1", "OutputName": Match.any_value()}}

    template.has_resource_properties(
        "AWS::Logs::DeliverySource",
        {
            "Name": "fast-eu-west-1-cdn-access",
            "ResourceArn": {"Fn::Join": ["", [f"arn:aws:cloudfront::{ACCOUNT}:distribution/", output_of("Media")]]},
        },
    )
    template.has_resource_properties(
        "AWS::Logs::DeliveryDestination", {"DestinationResourceArn": output_of("Data"), "OutputFormat": "parquet"}
    )
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "AlarmName": "fast-eu-west-1-cdn-5xx",
            "Namespace": "AWS/CloudFront",
            "MetricName": "5xxErrorRate",
            "Dimensions": [
                {"Name": "DistributionId", "Value": output_of("Media")},
                {"Name": "Region", "Value": "Global"},
            ],
            "AlarmActions": [{"Ref": Match.any_value()}],
        },
    )
    template.has_resource_properties("AWS::SNS::Topic", {"TopicName": "fast-eu-west-1-alerts"})
    template.has_resource_properties("AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"})
    assert "Fn::ImportValue" not in json.dumps(template.to_json())
    template.resource_count_is("AWS::Logs::LogGroup", 0)


def nag_violations(app: App, outdir: Path) -> list[Any]:
    Validations.of(app).add_plugins(AwsSolutionsChecks(app), ServerlessChecks(app))
    app.synth()
    reports = json.loads((outdir / "validation-report.json").read_text())["pluginReports"]
    return [v for r in reports for v in r.get("violations", [])]


def test_edge_stack_has_no_unacknowledged_nag_findings(tmp_path: Path) -> None:
    app = edge_deployment(tmp_path).node.root
    assert isinstance(app, App)

    assert nag_violations(app, tmp_path) == []


@pytest.mark.parametrize("domain", [None, "media.example.com"])
def test_no_unacknowledged_nag_findings(tmp_path: Path, domain: str | None) -> None:
    stack = new_stack(outdir=tmp_path)
    construct = media_distribution(stack, content_domain=domain, certificate_arn=CERTIFICATE if domain else None)
    CdnAccessLogs(
        stack,
        "AccessLogs",
        distribution=construct.distribution,
        logs_bucket=imported_bucket(stack, "logs"),
        name="fast",
    )
    app = stack.node.root
    assert isinstance(app, App)

    assert nag_violations(app, tmp_path) == []
