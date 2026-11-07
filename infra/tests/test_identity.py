"""The identity module's infrastructure (ADR-0018): the viewer pool, the ticket and consent table, the vid key, the
mint and the identity API with its routes and the guests' rate rule."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from aws_cdk.assertions import Template

from fast_channels.core.identity.ticket import REDEEM_CONDITION, REDEEM_UPDATE
from fast_channels.infra.config import ConfigError, FastChannelsConfig
from fast_channels.infra.constructs.identity.api import ACCESS_LOG, access_log_format

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

FASTM: dict[str, Any] = json.loads((Path(__file__).resolve().parents[2] / "stages" / "fastm.json").read_text())
"""Every module on, as ``STAGE=fastm`` deploys them."""
IDENTITY_OFF: dict[str, Any] = {
    **FASTM,
    "modules": {
        **FASTM["modules"],
        "identity": {"enabled": False},
        "viewer": {"enabled": False},
        "recs": {"enabled": False},
    },
}


@pytest.fixture(scope="module")
def identity(deployment: Callable[..., Any]) -> Template:
    stack = deployment(FASTM).stage.identity
    assert stack is not None and stack.stack_name == "FastChannels-fastm-Identity"
    return Template.from_stack(stack)


def _one(template: Template, kind: str) -> dict[str, Any]:
    [resource] = template.find_resources(kind).values()
    properties: dict[str, Any] = resource["Properties"]
    return properties


def _statements(template: Template) -> list[dict[str, Any]]:
    return [
        statement
        for policy in template.find_resources("AWS::IAM::Policy").values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def _methods(template: Template) -> dict[tuple[str, str], dict[str, Any]]:
    resources = template.find_resources("AWS::ApiGateway::Resource")
    paths = {logical: props["Properties"]["PathPart"] for logical, props in resources.items()}
    found = {}
    for method in template.find_resources("AWS::ApiGateway::Method").values():
        properties = method["Properties"]
        path = paths[properties["ResourceId"]["Ref"]]
        found[(path, properties["HttpMethod"])] = properties
    return found


def test_the_stack_exists_only_with_the_module(deployment: Callable[..., Any]) -> None:
    assert deployment(IDENTITY_OFF).stage.identity is None


def test_it_reads_the_media_bucket_by_parameter_and_changes_no_stack_but_media(
    deployment: Callable[..., Any], identity: Template
) -> None:
    """Media gets the edge rebuild (``test_identity_session.py``); no other stack of ``make deploy`` changes."""
    text = json.dumps(identity.to_json())
    assert "FastChannels-fastm-Data" not in text and "FastChannels-fastm-Media" not in text, "no weak reference"
    ssm = {p["Default"] for p in identity.to_json()["Parameters"].values() if p["Type"].startswith("AWS::SSM::")}
    assert ssm - {"/cdk-bootstrap/hnb659fds/version"} == {"/fastm/release/media-bucket"}
    on = deployment({**FASTM, "modules": {**FASTM["modules"], "recs": {"enabled": False}}})  # recs sets a flag in Ops
    off = deployment(IDENTITY_OFF)
    for name in ("data", "ops", "crawl", "operator", "models"):
        assert on.template(getattr(on.stage, name)).to_json() == off.template(getattr(off.stage, name)).to_json(), name


def test_the_module_needs_the_release_module() -> None:
    modules = {"modules": {"identity": {"enabled": True}}}
    with pytest.raises((ConfigError, ValueError), match="identity needs release"):
        FastChannelsConfig.model_validate(modules)


def test_the_viewer_pool_puts_the_playback_scope_in_access_tokens(identity: Template) -> None:
    pool = _one(identity, "AWS::Cognito::UserPool")
    assert pool["UserPoolName"] == "fastm-viewers"
    assert pool["UserPoolTier"] == "ESSENTIALS", "access-token customisation needs Essentials"
    assert pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True
    [persona] = [a for a in pool["Schema"] if a["Name"] == "persona"]
    assert (persona["AttributeDataType"], persona["Mutable"]) == ("String", True)
    trigger = pool["LambdaConfig"]["PreTokenGenerationConfig"]
    assert trigger["LambdaVersion"] == "V2_0"
    server = _one(identity, "AWS::Cognito::UserPoolResourceServer")
    assert (server["Identifier"], server["Scopes"][0]["ScopeName"]) == ("fast", "playback")
    client = _one(identity, "AWS::Cognito::UserPoolClient")
    assert client["GenerateSecret"] is False
    assert set(client["ExplicitAuthFlows"]) == {
        "ALLOW_USER_SRP_AUTH",
        "ALLOW_USER_PASSWORD_AUTH",
        "ALLOW_REFRESH_TOKEN_AUTH",
    }
    assert "custom:persona" in client["ReadAttributes"]


def test_the_ticket_table_and_the_vid_key(identity: Template) -> None:
    table = _one(identity, "AWS::DynamoDB::GlobalTable")
    assert table["TableName"] == "fastm-identity"
    assert table["KeySchema"] == [{"AttributeName": "pk", "KeyType": "HASH"}]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
    key = _one(identity, "AWS::KMS::Key")
    assert (key["KeySpec"], key["KeyUsage"]) == ("HMAC_256", "GENERATE_VERIFY_MAC")
    assert _one(identity, "AWS::KMS::Alias")["AliasName"] == "alias/fastm-identity-vid"


def test_the_mint_reads_plans_puts_and_reads_rows_and_macs_only(identity: Template) -> None:
    [mint] = [
        f["Properties"]
        for f in identity.find_resources("AWS::Lambda::Function").values()
        if f["Properties"]["Handler"] == "fast_channels.functions.identity.mint.handler"
    ]
    variables = mint["Environment"]["Variables"]
    assert variables["RELEASE_CHANNELS"] == "fastm-demo,fastm-kids" and variables["REDEEM_WINDOW_MS"] == "120000"
    assert variables["CHILD_DIRECTED_CHANNELS"] == "fastm-kids"
    granted = {
        a for s in _statements(identity) for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    }
    assert {"s3:GetObject", "dynamodb:PutItem", "dynamodb:GetItem", "kms:GenerateMac", "dynamodb:UpdateItem"} <= granted
    assert not {"dynamodb:Scan", "dynamodb:Query", "dynamodb:DeleteItem", "kms:Decrypt"} & granted
    [plans] = [s for s in _statements(identity) if s["Action"] == "s3:GetObject"]
    assert "plan/playback/*" in json.dumps(plans["Resource"])


def test_playback_takes_access_tokens_with_the_scope_and_answers_cors(identity: Template) -> None:
    methods = _methods(identity)
    post = methods[("playback", "POST")]
    assert (post["AuthorizationType"], post["AuthorizationScopes"]) == ("COGNITO_USER_POOLS", ["fast/playback"])
    assert post["Integration"]["Type"] == "AWS_PROXY"
    assert methods[("playback", "OPTIONS")]["AuthorizationType"] == "NONE", "the CORS preflight"
    responses = {
        r["Properties"]["ResponseType"]: r["Properties"]
        for r in identity.find_resources("AWS::ApiGateway::GatewayResponse").values()
    }
    for kind in ("DEFAULT_4XX", "DEFAULT_5XX"):
        assert responses[kind]["ResponseParameters"] == {"gatewayresponse.header.Access-Control-Allow-Origin": "'*'"}
    authorizer = _one(identity, "AWS::ApiGateway::Authorizer")
    assert authorizer["Type"] == "COGNITO_USER_POOLS"


def test_the_guest_mint_has_no_authorizer_and_consent_takes_the_access_token(identity: Template) -> None:
    methods = _methods(identity)
    guest = methods[("guest", "POST")]
    assert (guest["AuthorizationType"], guest["Integration"]["Type"]) == ("NONE", "AWS_PROXY")
    for verb in ("GET", "PUT"):
        consent = methods[("consent", verb)]
        assert (consent["AuthorizationType"], consent["AuthorizationScopes"]) == (
            "COGNITO_USER_POOLS",
            ["fast/playback"],
        )
    assert methods[("consent", "OPTIONS")]["AuthorizationType"] == "NONE", "the CORS preflight"
    assert ("guest", "OPTIONS") in methods


def test_a_web_acl_limits_guest_mints_per_address_and_keeps_no_requests(identity: Template) -> None:
    acl = _one(identity, "AWS::WAFv2::WebACL")
    assert acl["Scope"] == "REGIONAL" and acl["DefaultAction"] == {"Allow": {}}
    [rule] = acl["Rules"]
    rate = rule["Statement"]["RateBasedStatement"]
    assert (rule["Action"], rate["AggregateKeyType"], rate["Limit"], rate["EvaluationWindowSec"]) == (
        {"Block": {}},
        "IP",
        100,
        300,
    )
    match = rate["ScopeDownStatement"]["ByteMatchStatement"]
    assert (match["SearchString"], match["PositionalConstraint"]) == ("/v1/playback/guest", "EXACTLY")
    assert (
        not acl["VisibilityConfig"]["SampledRequestsEnabled"] and not rule["VisibilityConfig"]["SampledRequestsEnabled"]
    )
    association = _one(identity, "AWS::WAFv2::WebACLAssociation")
    assert "/stages/" in json.dumps(association["ResourceArn"])


def test_redeem_is_a_conditional_update_with_no_lambda(identity: Template) -> None:
    redeem = _methods(identity)[("redeem", "POST")]
    assert redeem["AuthorizationType"] == "NONE"
    integration = redeem["Integration"]
    assert integration["Type"] == "AWS" and "dynamodb:action/UpdateItem" in json.dumps(integration["Uri"])
    template = json.dumps(integration["RequestTemplates"]["application/json"])
    assert json.dumps(REDEEM_CONDITION)[1:-1] in template and json.dumps(REDEEM_UPDATE)[1:-1] in template
    assert "$context.requestTimeEpoch" in template
    codes = {r["StatusCode"]: r.get("SelectionPattern") for r in integration["IntegrationResponses"]}
    assert codes == {"200": None, "409": "4\\d{2}", "502": "5\\d{2}"}
    assert redeem["RequestValidatorId"] and redeem["RequestModels"]["application/json"]
    model = _one(identity, "AWS::ApiGateway::Model")
    assert model["Schema"]["properties"]["t"]["pattern"] == "^[A-Za-z0-9_-]{22}$"
    assert model["Schema"]["additionalProperties"] is False


def test_the_stage_is_throttled_and_logs_no_address(identity: Template) -> None:
    stage = _one(identity, "AWS::ApiGateway::Stage")
    assert stage["StageName"] == "v1"
    [settings] = stage["MethodSettings"]
    assert (settings["ThrottlingRateLimit"], settings["ThrottlingBurstLimit"]) == (50, 100)
    log_format = stage["AccessLogSetting"]["Format"]
    assert "sourceIp" not in log_format and "requestBody" not in log_format and "$context.status" in log_format


def test_the_parameters_and_outputs(identity: Template) -> None:
    names = {p["Properties"]["Name"] for p in identity.find_resources("AWS::SSM::Parameter").values()}
    assert names == {
        f"/fastm/identity/{name}"
        for name in (
            "user-pool-id",
            "client-id",
            "e2e-client-id",
            "issuer",
            "vid-key",
            "table",
            "playback-url",
            "redeem-url",
        )
    }
    assert set(identity.find_outputs("*")) >= {
        "UserPoolId",
        "UserPoolClientId",
        "PlaybackApiUrl",
        "RedeemUrl",
        "Region",
    }


def test_template_snapshot(identity: Template, normalized: Any, snapshot_json: SnapshotAssertion) -> None:
    assert normalized(identity) == snapshot_json


def test_an_access_log_line_is_json_and_names_no_quoted_variable() -> None:
    """``$context.error.messageString`` arrives quoted (``"Unauthorized"``): a format that quoted it again wrote
    ``"error": ""Unauthorized""``, which no JSON reader takes (seen on fastm, 2026-11-07). The format holds tokens
    only, so a line is the format with its variables substituted."""
    assert "messageString" not in access_log_format()
    line = access_log_format()
    for variable in ACCESS_LOG.values():
        line = line.replace(variable, "UNAUTHORIZED" if variable == "$context.error.responseType" else "x")
    assert json.loads(line)["error"] == "UNAUTHORIZED"
    assert set(json.loads(line)) == set(ACCESS_LOG)
