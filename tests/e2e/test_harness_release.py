"""Offline tests of the release harness (not marked e2e): the bundle with the lookup, the rig's template, and how the
viewers' breaks are read from the logs."""

from types import SimpleNamespace
from typing import Any

import harness_release
import pytest
from harness_release import LOOKUP_BUDGET_MS, LOOKUP_KEY, add_lookup, rig_template

from fast_channels.core.monetization.bundle import Bundle, ChannelInput, HookTree, SiblingContext, Step, build_bundle
from fast_channels.core.monetization.functions import (
    CacheScope,
    CustomOutputConfiguration,
    Function,
    FunctionType,
    Hook,
    function_stem,
)
from fast_channels.core.monetization.validators import validate_bundle

ORIGIN = "https://abc.lambda-url.us-east-1.on.aws/"


def stage_bundle(*contributions: Any) -> Bundle:
    channels = [
        ChannelInput(name="fastm-demo", video_content_source_url="https://ca.example.com/v1/channel/fastm-demo/"),
        ChannelInput(
            name="fastm-lab", video_content_source_url="https://ca.example.com/v1/channel/fastm-lab/", lab=True
        ),
    ]
    return build_bundle(
        prefix="fastm",
        channels=channels,
        content_base_url="https://d111111abcdef8.cloudfront.net",
        pbs_origin="https://d222222abcdef8.cloudfront.net",
        log_percent=100,
        stored_requests=["fast-pod", "fast-pod-kids"],
        grid_seconds=[30, 60, 90, 120],
        contributions=contributions,
    )


def pre_ads(budget: int) -> Any:
    """Another module's PRE_ADS step (as M17's content lookup) on every sibling."""

    def trees(sibling: SiblingContext) -> list[HookTree]:
        meta = Function(
            function_id=function_stem(sibling.prefix, "pa_meta"),
            function_type=FunctionType.CUSTOM_OUTPUT,
            custom_output_configuration=CustomOutputConfiguration(output={"temp.meta": "{% 'x' %}"}),
        )
        return [HookTree(hook=Hook.PRE_ADS, steps=(Step(function=meta),), outputs={}, budget_ms=budget)]

    return SimpleNamespace(name="crawl", hook_trees=trees)


@pytest.mark.parametrize("other", [None, 600])
def test_the_lookup_ends_the_pair_siblings_pre_ads_tree(other: int | None) -> None:
    bundle = stage_bundle(*([pre_ads(other)] if other else []))
    looked = add_lookup(bundle, f"{ORIGIN}lookup", {"x-fast-e2e-key": "k"})
    assert validate_bundle(looked) == [] and looked.digest != bundle.digest
    mapping = {s.name: s.playback.function_mapping or {} for s in looked.siblings}
    assert mapping["fastm-demo-p-a"][Hook.PRE_ADS] == mapping["fastm-demo-p-b"][Hook.PRE_ADS] == "fastm_pa_root"
    assert mapping["fastm-lab-p-b"][Hook.PRE_ADS] == "fastm_pa_root"
    root = looked.function("fastm_pa_root")
    assert root is not None and root.sequential_executor_configuration is not None
    executor = root.sequential_executor_configuration
    assert [r.function_id for r in executor.function_list][-1] == "fastm_pa_e2e_lookup"
    assert executor.timeout_milliseconds == min(2000, (other or 0) + LOOKUP_BUDGET_MS)
    lookup = looked.function("fastm_pa_e2e_lookup")
    assert lookup is not None and lookup.http_request_configuration is not None
    assert lookup.http_request_configuration.url == f"{ORIGIN}lookup/all"
    assert lookup.http_request_configuration.headers == {"x-fast-e2e-key": "k"}
    cache = lookup.http_request_configuration.cache
    assert cache is not None and cache.key == LOOKUP_KEY and cache.ttl_maximum_seconds == 1
    assert lookup.http_request_configuration.cache_scope is CacheScope.GLOBAL
    order = [f.function_id for f in looked.functions]
    assert all(order.index(r.function_id) < order.index(f.function_id) for f in looked.functions for r in f.steps)


def test_the_rig_opens_no_lambda_to_everyone_and_its_mint_reads_only_the_pointer() -> None:
    template = rig_template(application="app1", environment="env1", profile="prof1", layer="arn:layer:291")
    resources = template["Resources"]
    assert not [r for r in resources.values() if r["Type"] == "AWS::Lambda::Url"]
    [permission] = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::Lambda::Permission"]
    assert permission["Principal"] == "apigateway.amazonaws.com"
    assert permission["SourceArn"]["Fn::Sub"].endswith(":${Api}/*"), "only this API may invoke the origin"
    stage = resources["ApiStage"]["Properties"]
    assert stage["DefaultRouteSettings"] == {"ThrottlingBurstLimit": 40, "ThrottlingRateLimit": 20}
    assert template["Parameters"]["Key"]["NoEcho"] is True
    assert resources["Origin"]["Properties"]["Environment"]["Variables"]["KEY"] == {"Ref": "Key"}
    assert all("Tags" in resources[n]["Properties"] for n in ("Api", "ApiStage"))
    assert resources["Mint"]["Properties"]["Layers"] == ["arn:layer:291"]
    [statement] = [
        s
        for s in resources["MintRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        if "appconfig:GetLatestConfiguration" in s["Action"]
    ]
    assert statement["Resource"]["Fn::Sub"].endswith("application/app1/environment/env1/configuration/prof1")
    assert all(
        r["Properties"]["RetentionInDays"] == 1 for r in resources.values() if r["Type"] == "AWS::Logs::LogGroup"
    )


def test_a_break_failed_when_its_pre_ads_hook_erred(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "@timestamp": "2026-10-29 18:00:01.000",
            "sessionId": "s1",
            "eventType": "MAKING_ADS_REQUEST",
            "adBreakCorrelationId": "b1",
        },
        {"@timestamp": "2026-10-29 18:01:00.500", "sessionId": "s1", "eventType": "PRE_ADS_REQUEST_FUNCTION_ERROR"},
        {
            "@timestamp": "2026-10-29 18:01:01.000",
            "sessionId": "s1",
            "eventType": "MAKING_ADS_REQUEST",
            "adBreakCorrelationId": "b2",
        },
        {
            "@timestamp": "2026-10-29 18:02:01.000",
            "sessionId": "s2",
            "eventType": "PRE_ADS_REQUEST_HOOK_ERROR",
            "adBreakCorrelationId": "b3",
        },
        {
            "@timestamp": "2026-10-29 18:02:30.000",
            "sessionId": "s2",
            "eventType": "MAKING_ADS_REQUEST",
            "adBreakCorrelationId": "b3",
        },
    ]

    def insights(*_: Any, **__: Any) -> list[dict[str, str]]:
        return rows

    monkeypatch.setattr(harness_release, "insights", insights)
    found = harness_release.breaks(SimpleNamespace(prefix="fastm"), ["s1", "s2"], 0, 1)  # type: ignore[arg-type]
    assert [failed for _, failed in found["s1"]] == [False, True]
    assert [failed for _, failed in found["s2"]] == [True]
    assert found["s1"][0][0] == 1793296801.0
