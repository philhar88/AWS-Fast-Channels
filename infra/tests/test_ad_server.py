"""The ad-server mode's infrastructure (M8-09): the configuration, Prebid Cache's public read rule at the origin, the
private record of its write path, and the mock ad server behind its HTTP API."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.demand import NETWORK_CONTEXT
from fast_channels.infra.config import FastChannelsConfig

if TYPE_CHECKING:
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
    "cloudfront_domain": "d222222abcdef8.cloudfront.net",
    "target_group_arn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastm-pbs/0123456789abcd",
    "simulator_code_key": "simulator/0123456789abcdef.zip",
    "internal_alb_dns": "internal-fastm-pbs-Internal-1234567890.us-east-1.elb.amazonaws.com",
    "internal_alb_zone_id": "Z35SXDOTRQ7X7K",
}
EXTRA = {NETWORK_CONTEXT: NETWORK}
MOCK = {
    "prefix": "fastm",
    "modules.ops.enabled": True,
    "modules.demand.enabled": True,
    "modules.demand.simulator": "lambda",
    "modules.demand.ad_server": "mock",
    "modules.pds.enabled": True,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
}
GAM = {
    **MOCK,
    "modules.demand.ad_server": "gam",
    "modules.demand.ad_server_tag": "https://pubads.g.doubleclick.net/gampad/ads?iu=/1/f&cust_params={prebid}",
}


def resources(template: Any, kind: str) -> list[dict[str, Any]]:
    return [r["Properties"] for r in template.find_resources(kind).values()]


# --- configuration -------------------------------------------------------------------------------------------------


def _config(**demand: Any) -> FastChannelsConfig:
    modules = {
        "ops": {"enabled": True},
        "demand": {"enabled": True, **demand},
        "pds": {"enabled": True},
        "release": {"enabled": True},
    }
    return FastChannelsConfig.model_validate({"modules": modules})


def test_the_ad_server_is_off_by_default() -> None:
    demand = FastChannelsConfig().modules.demand
    assert (demand.ad_server.value, demand.ad_server_tag, demand.pod_cache_seconds) == ("none", "", 300)


@pytest.mark.parametrize(
    ("demand", "message"),
    [
        ({"ad_server": "gam"}, "needs ad_server_tag"),
        ({"ad_server": "freewheel", "ad_server_tag": "https://x.example/ad?{prebid}{prebid}"}, "needs ad_server_tag"),
        ({"ad_server": "gam", "ad_server_tag": "http://x.example/ad?{prebid}"}, "needs ad_server_tag"),
        ({"ad_server": "mock", "ad_server_tag": "https://x.example/ad?{prebid}"}, "only for gam and freewheel"),
        ({"pod_cache_seconds": 10}, "greater than or equal to 30"),
    ],
)
def test_invalid_ad_server_settings(demand: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _config(**demand)


def test_an_ad_server_needs_the_release_module() -> None:
    with pytest.raises(ValueError, match="needs release"):
        FastChannelsConfig.model_validate(
            {"modules": {"ops": {"enabled": True}, "demand": {"enabled": True, "ad_server": "mock"}}}
        )
    assert _config(ad_server="mock").modules.demand.ad_server.value == "mock"


# --- the origin rule ------------------------------------------------------------------------------------------------


def test_the_origin_opens_only_cache_reads_by_uuid(deployment: GetDeployment) -> None:
    d = deployment(MOCK)
    [group] = resources(d.template(d.stage.demand_edge), "AWS::WAFv2::RuleGroup")
    rules = {rule["Name"]: rule for rule in group["Rules"]}

    assert list(rules) == ["fast-origin-required", "fast-paths", "fast-rate", "fast-cache-read"]
    assert group["Capacity"] == 50, "a rule group's capacity is immutable: the guidance's web ACL references it"
    open_paths = rules["fast-paths"]["Statement"]["NotStatement"]["Statement"]["OrStatement"]["Statements"]
    assert [s["ByteMatchStatement"]["SearchString"] for s in open_paths] == ["/openrtb2/auction", "/status", "/cache"]
    read = rules["fast-cache-read"]
    assert (read["Priority"], read["Action"]) == (3, {"Block": {}})
    path, not_allowed = read["Statement"]["AndStatement"]["Statements"]
    assert (path["ByteMatchStatement"]["SearchString"], path["ByteMatchStatement"]["PositionalConstraint"]) == (
        "/cache",
        "EXACTLY",
    )
    methods, uuid = not_allowed["NotStatement"]["Statement"]["AndStatement"]["Statements"]
    assert [m["ByteMatchStatement"]["SearchString"] for m in methods["OrStatement"]["Statements"]] == ["GET", "HEAD"]
    assert uuid["RegexMatchStatement"]["FieldToMatch"] == {"SingleQueryArgument": {"Name": "uuid"}}
    assert uuid["RegexMatchStatement"]["RegexString"] == (
        "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )


def test_without_an_ad_server_the_cache_stays_closed(deployment: GetDeployment) -> None:
    d = deployment({**MOCK, "modules.demand.ad_server": "none"})
    [group] = resources(d.template(d.stage.demand_edge), "AWS::WAFv2::RuleGroup")

    assert [rule["Name"] for rule in group["Rules"]] == ["fast-origin-required", "fast-paths", "fast-rate"]


# --- beside the guidance --------------------------------------------------------------------------------------------


def test_the_cache_write_path_is_a_private_record_of_the_internal_balancer(deployment: GetDeployment) -> None:
    for overrides in (MOCK, GAM):
        d = deployment(overrides, extra=EXTRA)
        records = {r["Name"]: r for r in resources(d.template(d.stage.demand), "AWS::Route53::RecordSet")}

        assert records["cache.fastm.fast.internal."]["AliasTarget"] == {
            "DNSName": f"dualstack.{NETWORK['internal_alb_dns']}",
            "HostedZoneId": NETWORK["internal_alb_zone_id"],
        }


def test_an_ad_server_needs_the_internal_balancer(deployment: GetDeployment) -> None:
    with pytest.raises(ValueError, match="internal load balancer"):
        deployment(MOCK, extra={NETWORK_CONTEXT: {**NETWORK, "internal_alb_dns": None}})


def test_the_mock_answers_mediatailor_through_an_api_scoped_to_it(deployment: GetDeployment) -> None:
    d = deployment(MOCK, extra=EXTRA)
    template = d.template(d.stage.demand)
    [api] = resources(template, "AWS::ApiGatewayV2::Api")
    [route] = resources(template, "AWS::ApiGatewayV2::Route")
    [stage] = resources(template, "AWS::ApiGatewayV2::Stage")
    functions = {f["Handler"]: f for f in resources(template, "AWS::Lambda::Function") if "Handler" in f}
    mock = functions["fast_channels.functions.demand.ad_server.handler"]
    permissions = [
        p for p in resources(template, "AWS::Lambda::Permission") if p["Principal"] == "apigateway.amazonaws.com"
    ]
    [parameter] = [p for p in resources(template, "AWS::SSM::Parameter") if p["Name"] == "/fastm/demand/ad-server-url"]

    assert (api["Name"], api["ProtocolType"]) == ("fastm-demand-ad-server", "HTTP")
    assert route["RouteKey"] == "GET /ads"
    assert stage["DefaultRouteSettings"] == {"ThrottlingBurstLimit": 400, "ThrottlingRateLimit": 200}
    variables = mock["Environment"]["Variables"]
    assert variables["CACHE_URL"] == "https://d222222abcdef8.cloudfront.net/cache"
    assert variables["ORIGIN_SECRETS"] == "fastm/demand/origin-a,fastm/demand/origin-b"
    assert variables["ORIGIN_SECRET_REGION"] == "us-east-1"
    assert permissions, "the integration's permission"
    assert all("SourceArn" in p for p in permissions), "never open to everyone (the account guardrail)"
    assert "Value" in parameter
    text = json.dumps(template.to_json())
    assert "secretsmanager:GetSecretValue" in text


def test_a_third_party_ad_server_has_no_mock(deployment: GetDeployment) -> None:
    d = deployment(GAM, extra=EXTRA)
    template = d.template(d.stage.demand)

    assert not template.find_resources("AWS::ApiGatewayV2::Api")
    assert d.stage.demand.ad_server is None


@pytest.mark.parametrize("overrides", [MOCK, GAM], ids=["mock", "gam"])
def test_the_bundle_routes_the_pair_to_the_ad_server(deployment: GetDeployment, overrides: dict[str, Any]) -> None:
    """The bundle is validated at synth with the route: the pair siblings call the ad server, each after its own
    auction, and the probe stays on Prebid Server."""
    bundle = deployment(overrides, extra=EXTRA).stage.release.rendered.bundle
    routes = {s.name: s.route.value for s in bundle.siblings}
    assert routes == {  # the stage's lab channel has a pair too
        f"fastm-{channel}-p-{letter}": route
        for channel in ("demo", "lab")
        for letter, route in (("a", "ad_server"), ("b", "ad_server"), ("z", "pbs"))
    }
    assert {f.function_id for f in bundle.functions} >= {
        "fastm_demo_p_a_pa_auction",
        "fastm_demo_p_b_pa_auction",
        "fastm_ps_hb",
    }
    assert {"fast-adsrv-pod", "fast-adsrv-pod-kids"} <= set(bundle.stored_requests)
    a = next(s for s in bundle.siblings if s.letter == "a")
    if overrides is MOCK:  # the mock's URL is the Demand stack's parameter, resolved at deploy
        assert a.playback.ad_decision_server_url.startswith("https://cdk-token-")
        assert a.playback.sends_origin
    else:
        assert a.playback.ad_decision_server_url.startswith("https://pubads.g.doubleclick.net/gampad/ads?")
        assert not a.playback.sends_origin and not a.playback.headers


def test_without_an_ad_server_the_bundle_is_the_direct_route(deployment: GetDeployment) -> None:
    bundle = deployment({**MOCK, "modules.demand.ad_server": "none"}, extra=EXTRA).stage.release.rendered.bundle
    assert {s.route.value for s in bundle.siblings} == {"pbs"}
    assert not [s for s in bundle.stored_requests if s.startswith("fast-adsrv-")]
    assert not [f for f in bundle.functions if f.function_id.endswith(("pa_auction", "ps_hb"))]


def test_template_snapshot(
    deployment: GetDeployment, normalized: Callable[..., Any], snapshot_json: SnapshotAssertion
) -> None:
    d = deployment(MOCK, extra=EXTRA)

    assert normalized(d.template(d.stage.demand)) == snapshot_json
