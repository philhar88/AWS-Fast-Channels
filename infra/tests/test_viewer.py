"""The viewer module's stack: the reference viewer's site and config.json, and the demo's ad panel API."""

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from aws_cdk.assertions import Match, Template

from fast_channels.core.personas import PERSONAS
from fast_channels.infra import stage
from fast_channels.infra.constructs.viewer import ViewerConfig
from fast_channels.infra.stacks.viewer import ViewerStack

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
API = "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/v1/playback"


def viewer(
    fresh_deployment: Callable[..., Any], *, ad_panel: bool, outdir: Path | None = None
) -> tuple[Any, ViewerStack]:
    """The fastm stage and its Viewer stack."""
    d = fresh_deployment({**FASTM, "modules.viewer.ad_panel": ad_panel}, outdir=outdir)
    assert d.stage.viewer is not None
    return d, d.stage.viewer


def parameter(template: Template, logical_id: str) -> str:
    """The SSM parameter name behind a template parameter ``logical_id``."""
    return str(template.to_json()["Parameters"][logical_id]["Default"])


def joined(value: Any) -> str:
    """A template string with its ``Fn::Join`` flattened and every other intrinsic as ``${...}``."""
    match value:
        case str():
            return value
        case {"Fn::Join": [separator, parts]}:
            return str(separator).join(joined(part) for part in parts)
        case {"Ref": name}:
            return f"${{{name}}}"
        case _:
            return f"${{{json.dumps(value)}}}"


def csp(template: Template) -> str:
    [policy] = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()
    value = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]["ContentSecurityPolicy"]
    return joined(value["ContentSecurityPolicy"])


def test_the_site_is_private_behind_its_own_distribution_with_the_viewer_csp(
    fresh_deployment: Callable[..., Any],
) -> None:
    _, stack = viewer(fresh_deployment, ad_panel=False)
    template = Template.from_stack(stack)

    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {"PublicAccessBlockConfiguration": Match.object_like({"BlockPublicAcls": True, "RestrictPublicBuckets": True})},
    )
    policy = csp(template)
    assert "https://cognito-idp.us-east-1.${AWS::URLSuffix}" in policy
    # The playback API's origin, from the identity module's SSM parameter at deploy time.
    assert "/fastm/identity/playback-url" in {
        parameter(template, p) for p in template.to_json()["Parameters"] if p.startswith("SsmParameterValue")
    }
    assert "bedrock-agentcore" not in policy
    assert "script-src 'self'" in policy
    template.resource_count_is("AWS::ApiGatewayV2::Api", 0)
    template.resource_count_is("AWS::Lambda::Function", 2)  # the BucketDeployment handler and autoDeleteObjects


def test_the_ad_panel_admits_only_viewer_access_tokens_and_reads_only_the_pds_log_group(
    fresh_deployment: Callable[..., Any],
) -> None:
    _, stack = viewer(fresh_deployment, ad_panel=True)
    template = Template.from_stack(stack)

    [authorizer] = template.find_resources("AWS::ApiGatewayV2::Authorizer").values()
    jwt = authorizer["Properties"]["JwtConfiguration"]
    assert authorizer["Properties"]["AuthorizerType"] == "JWT"
    assert parameter(template, jwt["Audience"][0]["Ref"]) == "/fastm/identity/client-id"
    assert parameter(template, jwt["Issuer"]["Ref"]) == "/fastm/identity/issuer"
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {
            "RouteKey": "GET /sessions/{sid}/breaks",
            "AuthorizationType": "JWT",
            "AuthorizationScopes": ["fast/playback"],
        },
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "fast_channels.functions.viewer.ad_panel.handler",
            "ReservedConcurrentExecutions": 5,
            "Environment": {"Variables": Match.object_like({"DECISION_LOG_GROUP": "/aws/ecs/fastm-pds"})},
        },
    )
    statements = [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]
    start = next(s for s in statements if s["Action"] == "logs:StartQuery")
    assert "log-group:/aws/ecs/fastm-pds" in json.dumps(start["Resource"])
    mac = next(s for s in statements if s["Action"] == "kms:GenerateMac")
    assert mac["Condition"] == {"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}
    assert not [s for s in statements if "logs:FilterLogEvents" in json.dumps(s["Action"])]
    assert "ApiEndpoint" in csp(template)  # the panel API's origin, from its endpoint


def test_config_publishes_public_persona_fields_and_marks_monetized_channels(
    fresh_deployment: Callable[..., Any],
) -> None:
    _, stack = viewer(fresh_deployment, ad_panel=True)
    document = ViewerConfig(
        region="us-east-1",
        user_pool_client_id="c",
        playback_url=API,
        cdn_base_url="https://d1.cloudfront.net",
        ad_panel_url="https://panel.example/",
        channels=stack.channels,
        monetized=frozenset({"fastm-demo"}),
        personas=PERSONAS,
    ).document()

    assert document["playbackUrl"] == API
    assert document["planBaseUrl"] == "https://d1.cloudfront.net/plan/playback"
    assert document["epgBaseUrl"] == "https://d1.cloudfront.net/epg"
    assert [(c["name"], c["monetized"]) for c in document["channels"]] == [("fastm-demo", True)]
    assert {p["id"] for p in document["personas"]} == {p.id for p in PERSONAS}
    assert all(set(p) == {"id", "name", "bio", "email", "signIn", "genres"} for p in document["personas"])
    assert "light" not in json.dumps(document["personas"])  # no tier reaches the page


def test_the_build_is_deployed_beside_config_json(fresh_deployment: Callable[..., Any], tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html>")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(stage, "ViewerStack", partial(ViewerStack, dist=tmp_path))
        _, built = viewer(fresh_deployment, ad_panel=False)
        patch.setattr(stage, "ViewerStack", partial(ViewerStack, dist=tmp_path / "missing"))
        _, missing = viewer(fresh_deployment, ad_panel=False)

    # The assets, then index.html and config.json; with no build (bundling is off here) a placeholder page only.
    Template.from_stack(built).resource_count_is("Custom::CDKBucketDeployment", 2)
    Template.from_stack(missing).resource_count_is("Custom::CDKBucketDeployment", 1)


@pytest.mark.parametrize("ad_panel", [False, True])
def test_no_unacknowledged_findings(fresh_deployment: Callable[..., Any], tmp_path: Path, ad_panel: bool) -> None:
    d, _ = viewer(fresh_deployment, ad_panel=ad_panel, outdir=tmp_path)

    d.app.synth()

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []


def test_the_stage_builds_the_viewer_after_identity(deployment: Callable[..., Any]) -> None:
    d = deployment(FASTM)
    assert d.stage.viewer is not None and d.stage.identity is not None
    assert d.stage.identity in d.stage.viewer.dependencies
    # Read through SSM, never a cross-stack reference: the Identity template does not change for the viewer.
    assert not [
        o for o in Template.from_stack(d.stage.identity).to_json().get("Outputs", {}) if o.startswith("PublishOutput")
    ]
    assert deployment().stage.viewer is None  # off in the shipped configuration
