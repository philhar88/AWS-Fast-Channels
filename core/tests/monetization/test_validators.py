"""Validators R-F1 … R-S1 and R-A1/R-A2: each rejects its Phase-0 counter-example, and every branch is exercised."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from fast_channels.core.monetization import playback as pb
from fast_channels.core.monetization import validators as v
from fast_channels.core.monetization.bundle import (
    ROLLOUTS,
    Bundle,
    HookTree,
    Rollout,
    RolloutTier,
    Step,
    bundle_document,
)
from fast_channels.core.monetization.functions import (
    Cache,
    CustomOutputConfiguration,
    Function,
    FunctionRef,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    ps_ctl,
    sequential_executor,
)
from fast_channels.core.monetization.validators import Rule

# --- the Phase-0 counter-examples (BUILD-PLAN M3-01 acceptance) -------------------------------------------------------


def test_pad_is_not_on_the_allow_list(bundle: Bundle, kit: SimpleNamespace) -> None:
    """P0-20 §4.5: PutFunction accepted ``$pad``; the hook failed at run time with RESTRICTION_ERROR."""

    def change(document: dict[str, Any]) -> None:
        output = kit.function_doc(document, "fastm_ps_ctl")["CustomOutputConfiguration"]["Output"]
        output["aws.preroll"] = "{% $pad('', 5, 'c') %}"

    found = v.validate_bundle(kit.edit(bundle, change))
    assert "function fastm_ps_ctl Output[aws.preroll]: $pad not on the allow-list (it fails at run time, P0-20)" in (
        kit.messages(found, Rule.F2)
    )


def test_a_1001_character_expression(bundle: Bundle, kit: SimpleNamespace) -> None:
    """P0-20 §4.5: the service accepted 1,001 and 1,500 characters; the documented limit is 1,000."""
    for length, rejected in ((1000, False), (1001, True)):

        def change(document: dict[str, Any], length: int = length) -> None:
            output = kit.function_doc(document, "fastm_ps_ctl")["CustomOutputConfiguration"]["Output"]
            output["temp.long"] = "{% '" + "x" * (length - 8) + "' %}"

        found = v.validate_bundle(kit.edit(bundle, change))
        assert any("expression of 1001 characters" in m for m in kit.messages(found, Rule.F1)) is rejected


def test_an_unquoted_player_parameter_in_the_body() -> None:
    """P0-20 §4.14: an unresolved unquoted value made the body invalid and MediaTailor sent no ADS request."""
    found = v.check_body_template('{"ext":{"x":1},"len":{{player_params.x}}}')
    assert [x.message for x in found] == [
        "uses ['player_params.x']; the body takes only "
        "['scte.event_id', 'session.avail_duration_secs', 'session.uuid'] and constants",
        "not valid JSON when a variable renders empty (MediaTailor skips the call)",
    ]


def test_the_user_agent_in_the_body() -> None:
    """``session.user_agent`` is client-influenced; a quote in it would suppress the ADS call."""
    found = v.check_body_template('{"device":{"ua":"{{session.user_agent}}"}}')
    assert v.Rule.P1 in {x.rule for x in found}
    assert "session.user_agent" in found[0].message


def test_a_cache_key_with_player_params(bundle: Bundle, kit: SimpleNamespace) -> None:
    """P0-10: the cache is per configuration and partitioned about 4 ways; a per-viewer key serves stale state."""
    meta = kit.pa_meta(
        cache=Cache(ttl_minimum_seconds=300, ttl_maximum_seconds=300, key="{% 'meta|v1|' & player_params.vid %}")
    )
    found = v.check_function(meta, prefix="fastm")
    assert kit.messages(found, Rule.F4) == [
        "function fastm_pa_meta Cache.Key: reads ['player_params.vid']; every session of the configuration shares the "
        "cache"
    ]


def test_a_channel_wildcard_in_the_policy() -> None:
    """P0-08: ``<ch>*`` admitted another channel's sibling 51 of 72 times."""
    found = v.check_channel_policy(_policy(["fastm-demo", "fastm-demo*"]), channel="fastm-demo")
    assert {x.rule for x in found} == {Rule.C1}
    assert any("no '*'" in x.message for x in found)


def test_all_at_once() -> None:
    """P0-08: a 0-minute deployment completed with its monitor in INSUFFICIENT_DATA; it is never monitored."""
    all_at_once = Rollout(
        name="AppConfig.AllAtOnce",
        growth_type="LINEAR",
        growth_factor=100,
        deployment_duration_in_minutes=0,
        final_bake_time_in_minutes=0,
    )
    assert [x.message for x in v.check_rollout(all_at_once)] == [
        "never all at once: a 0-minute deployment ignores its monitors",
        "the first step exposes 100.0%; at most 10%",
        "every step lasts longer than 2 minutes (an alarm needs 90-118 s)",
        "the final bake lasts longer than 2 minutes",
    ]


# --- helpers ----------------------------------------------------------------------------------------------------------


def _policy(arns: list[str] | str, *, account: bool = True) -> dict[str, Any]:
    prefix = "arn:aws:mediatailor:us-east-1:123456789012:playbackConfiguration/"
    values = [f"{prefix}{a}" for a in arns] if isinstance(arns, list) else arns
    condition: dict[str, Any] = {"ArnLike": {"aws:SourceArn": values}}
    if account:
        condition["StringEquals"] = {"aws:SourceAccount": "123456789012"}
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "mediatailor.amazonaws.com"},
                "Action": "mediatailor:GetManifest",
                "Resource": "arn:aws:mediatailor:us-east-1:123456789012:channel/fastm-demo",
                "Condition": condition,
            }
        ],
    }


def _tree(
    kit: SimpleNamespace, hook: Hook, *steps: Function, outputs: dict[str, str] | None = None, budget: int = 500
) -> Bundle:
    """A bundle with one extra contribution at ``hook`` on every sibling."""
    tree = HookTree(hook=hook, steps=tuple(Step(function=s) for s in steps), outputs=outputs or {}, budget_ms=budget)
    return kit.make_bundle(contributions=[kit.Contribution("t", lambda _: [tree])])


def _output(stem: str, output: dict[str, str]) -> Function:
    return Function(
        function_id=stem,
        function_type=FunctionType.CUSTOM_OUTPUT,
        custom_output_configuration=CustomOutputConfiguration(output=output),
    )


def _http(stem: str, **configuration: Any) -> Function:
    return Function(
        function_id=stem,
        function_type=FunctionType.HTTP_REQUEST,
        http_request_configuration=HttpRequestConfiguration(
            **(
                {"method_type": "GET", "url": "https://x.example.com/a", "request_timeout_milliseconds": 200}
                | configuration
            )
        ),
    )


# --- R-F1 … R-F4: one document ----------------------------------------------------------------------------------------


def test_function_ids_and_counts(kit: SimpleNamespace) -> None:
    for stem, message in (
        ("fastm_ps-ctl", "[A-Za-z0-9_] only"),
        ("fastm_ps_ctl_r0001", "bundle ids are stems"),
        ("fast_ps_ctl", "start with fastm_"),
    ):
        found = v.check_function(ps_ctl("fastm").model_copy(update={"function_id": stem}), prefix="fastm")
        assert any(message in m for m in kit.messages(found, Rule.F1)), stem
    many = _output("fastm_many", {f"temp.k{n}": "{% 'x' %}" for n in range(21)})
    assert kit.messages(v.check_function(many, prefix="fastm"), Rule.F1) == [
        "function fastm_many: 21 outputs; at most 20"
    ]
    tagged = ps_ctl("fastm").model_copy(update={"tags": {"fast:app": "fastm"}})
    assert kit.messages(v.check_function(tagged, prefix="fastm"), Rule.F3) == [
        "function fastm_ps_ctl: tags are set at apply, when the id is created"
    ]


def test_executor_limits(kit: SimpleNamespace) -> None:
    refs = [FunctionRef(function_id=f"fastm_s{n}") for n in range(11)]
    big = sequential_executor("fastm_big", steps=refs, output={}, timeout_ms=99, description="")
    found = v.check_function(big, prefix="fastm")
    assert kit.messages(found, Rule.F1) == [
        "function fastm_big: 11 steps; an executor runs 1-10",
        "function fastm_big: TimeoutMilliseconds must be 100-2,000",
    ]
    constant = sequential_executor(
        "fastm_c",
        steps=[FunctionRef(function_id="fastm_s", run_condition="true")],
        output={},
        timeout_ms=100,
        description="",
    )
    assert kit.messages(v.check_function(constant, prefix="fastm"), Rule.F3) == [
        "function fastm_c FunctionList[0]: RunCondition is a {% … %} expression"
    ]


def test_request_limits(kit: SimpleNamespace) -> None:
    function = _http(
        "fastm_req",
        url="ftp://x",
        request_timeout_milliseconds=2500,
        method_type="POST",
        body="x" * (64 * 1024 + 1),
        headers={"Host": "x", "h" * 257: "v", "X-Long": "v" * 8193, **{f"X-{n}": "v" for n in range(48)}},
    )
    found = kit.messages(v.check_function(function, prefix="fastm"), Rule.F1)
    assert "function fastm_req: RequestTimeoutMilliseconds must be 100-2,000" in found
    assert "function fastm_req Url: a constant URL is http(s) and at most 2048 characters" in found
    assert "function fastm_req Body: a request body is at most 64 KB" in found
    assert "function fastm_req Headers: 51 headers; at most 50" in found
    assert "function fastm_req Headers[Host]: a restricted header is dropped at run time" in found
    assert sum("header names ≤256 and values ≤8,192" in m for m in found) == 2
    assert v.check_function(_http("fastm_ok", url="https://" + "x" * 2040), prefix="fastm") == []


def test_cache_rules(kit: SimpleNamespace) -> None:
    def cached(**cache: Any) -> list[str]:
        settings = {"ttl_minimum_seconds": 300, "ttl_maximum_seconds": 300} | cache
        found = v.check_function(kit.pa_meta(cache=Cache(**settings)), prefix="fastm")
        return [x.message for x in found if x.rule in {Rule.F1, Rule.F3, Rule.F4}]

    assert cached(key="{% 'meta|v1|' & $string(scte.segmentation_upid) %}") == []
    assert cached(ttl_minimum_seconds=400, ttl_maximum_seconds=10, key="{% 'meta|v1|' %}") == [
        "TTLs are 1-300 seconds",
        "TtlMaximumSeconds must be at least TtlMinimumSeconds",
    ]
    no_key = "a cached function sets Key, an expression ('<name>|v<N>|' & …)"
    assert cached() == [no_key]
    assert cached(key="meta|v1|static") == [no_key]
    assert cached(key="{% $string(scte.segmentation_upid) %}") == [
        "starts with the literal '<name>|v<N>|': cache entries cannot be invalidated"
    ]
    assert cached(key="{% ('meta|v2|' & scte.event_id) %}") == []
    assert cached(key="{% 'meta|v1|' & session.uuid & avail.random %}") == [
        "reads ['avail.random', 'session.uuid']; every session of the configuration shares the cache"
    ]
    assert cached(key="{% 'meta|v1|' & ?? %}") == ["the validators cannot read this expression: unexpected '??' at 14"]


def test_unreadable_expressions_are_refused(kit: SimpleNamespace) -> None:
    function = _output("fastm_bad", {"temp.x": "{% a.* %}"})
    assert kit.messages(v.check_function(function, prefix="fastm"), Rule.F3) == [
        "function fastm_bad Output[temp.x]: the validators cannot read this expression: unexpected '*' at 3"
    ]


# --- trees ------------------------------------------------------------------------------------------------------------


def test_references_and_nesting(bundle: Bundle, kit: SimpleNamespace) -> None:
    def missing_root(document: dict[str, Any]) -> None:
        kit.playback_doc(document)["FunctionMapping"]["PRE_SESSION_INITIALIZATION"] = "fastm_nope"

    found = v.validate_bundle(kit.edit(bundle, missing_root))
    assert (
        "sibling fastm-demo-p-a: maps PRE_SESSION_INITIALIZATION to 'fastm_nope', which the bundle does not hold"
        in (kit.messages(found, Rule.F3))
    )

    def missing_step(document: dict[str, Any]) -> None:
        root = kit.function_doc(document, "fastm_ps_root")["SequentialExecutorConfiguration"]
        root["FunctionList"].append({"FunctionId": "fastm_gone"})
        root["FunctionList"].append({"FunctionId": "fastm_ps_root"})  # an executor as a step: depth 3

    found = v.validate_bundle(kit.edit(bundle, missing_step))
    assert "function fastm_ps_root: runs 'fastm_gone', not in the bundle" in kit.messages(found, Rule.F3)
    assert "function fastm_ps_root: step 'fastm_ps_root' is an executor; nesting depth ≤2" in kit.messages(
        found, Rule.F1
    )

    def orphan(document: dict[str, Any]) -> None:
        document["functions"].insert(0, _output("fastm_orphan", {"temp.x": "{% 'x' %}"}).api())

    found = v.validate_bundle(kit.edit(bundle, orphan))
    assert kit.messages(found, Rule.F3) == [
        "function fastm_orphan: no sibling maps it; the bundle holds only mapped trees"
    ]


def test_output_bounds_and_writable_keys(kit: SimpleNamespace) -> None:
    unbounded = kit.pa_meta(output={"temp.c_hdr": "{% response.body.hdr %}"})
    found = v.validate_bundle(_tree(kit, Hook.PRE_ADS, unbounded, outputs={"adsRequest.headers.x": "{% temp.c_hdr %}"}))
    assert kit.messages(found, Rule.F1) == [
        "function fastm_pa_meta Output[temp.c_hdr]: cannot bound this value; cap it ($substring)",
        "function fastm_pa_root Output[adsRequest.headers.x]: cannot bound this value; cap it ($substring)",
    ]
    long_literal = _output("fastm_long", {"temp.x": "{% 'x' %}", "temp.y": "y" * 1001})
    found = v.validate_bundle(_tree(kit, Hook.PRE_ADS, long_literal))
    assert kit.messages(found, Rule.F1) == ["function fastm_long Output[temp.y]: may reach 1001; at most 1000"]
    wrong = _output("fastm_w", {"adsResponse.ads": "{% [] %}", "temp.ok": "{% 'x' %}"})
    found = v.validate_bundle(_tree(kit, Hook.PRE_ADS, wrong))
    assert kit.messages(found, Rule.F3) == ["function fastm_w Output[adsResponse.ads]: not writable at PRE_ADS_REQUEST"]


def test_executions_per_hook(bundle: Bundle, kit: SimpleNamespace) -> None:
    def twenty(document: dict[str, Any]) -> None:
        root = kit.function_doc(document, "fastm_ps_root")["SequentialExecutorConfiguration"]
        root["FunctionList"] = [{"FunctionId": "fastm_ps_ctl"}] * 20

    found = v.validate_bundle(kit.edit(bundle, twenty))
    assert "PRE_SESSION_INITIALIZATION tree fastm_ps_root: 21 executions; at most 20 per hook" in kit.messages(
        found, Rule.F1
    )


def test_pre_ads_lookup_reads_only_the_break_ids(kit: SimpleNamespace) -> None:
    leaky = kit.pa_meta(url="{% 'https://x/' & session.uuid %}", headers={"X-Vid": "{% player_params.vid %}"})
    found = v.validate_bundle(
        kit.make_bundle(
            contributions=[
                kit.crawl(url=leaky.http_request_configuration.url, headers=leaky.http_request_configuration.headers)
            ]
        )
    )
    assert kit.messages(found, Rule.F5) == [
        "function fastm_pa_meta Url: reads ['session.uuid']; a PRE_ADS lookup reads only the SCTE-35 ids and constants",
        "function fastm_pa_meta Headers[X-Vid]: reads ['player_params.vid']; a PRE_ADS lookup reads only the SCTE-35 "
        "ids and constants",
    ]
    body = _http("fastm_body", method_type="POST", body="{% $string({'u': scte.segmentation_upid}) %}")
    assert v.validate_bundle(_tree(kit, Hook.PRE_ADS, body)) == []


def test_pre_ads_never_rewrites_the_ads_request(kit: SimpleNamespace) -> None:
    rewrite = _output(
        "fastm_rw",
        {
            "adsRequest.body": "{% '{}' %}",
            "session.uuid": "{% 'x' %}",
            "adsRequest.headers.x-ok": "{% 'x' %}",
            "player_params.t": "{% 'stolen' %}",
        },
    )
    found = v.validate_bundle(_tree(kit, Hook.PRE_ADS, rewrite))
    assert kit.messages(found, Rule.P1) == [
        "function fastm_rw Output[adsRequest.body]: a PRE_ADS function never rewrites the ADS URL, the body or the "
        "variables they read",
        "function fastm_rw Output[session.uuid]: a PRE_ADS function never rewrites the ADS URL, the body or the "
        "variables they read",
    ]
    assert kit.messages(found, Rule.P2) == ["function fastm_rw: writes player_params.t, the ticket"]


# --- R-F6: PRE_SESSION ------------------------------------------------------------------------------------------------


def test_pre_session_writes_constants_only(kit: SimpleNamespace) -> None:
    bad = _output(
        "fastm_bad",
        {
            "player_params.t": "{% 'x' %}",
            "manifest.aws.mediatailor.channel.audienceId": "{% 'a' %}",
            "player_params.vid": "{% session.uuid %}",
        },
    )
    found = kit.messages(v.validate_bundle(_tree(kit, Hook.PRE_SESSION, bad, budget=100)), Rule.F6)
    assert found[:3] == [
        "function fastm_bad Output[player_params.t]: PRE_SESSION never targets the ticket",
        "function fastm_bad Output[manifest.aws.mediatailor.channel.audienceId]: PRE_SESSION never writes manifest.* "
        "(it reaches the origin)",
        "function fastm_bad Output[player_params.vid]: a PRE_SESSION CUSTOM_OUTPUT writes literals only",
    ]
    assert (
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: player_params.t is written but not re-exported, so it is "
        "discarded" in found
    )


def test_pre_session_player_params_size(kit: SimpleNamespace) -> None:
    sized = _output("fastm_pp", {"player_params.a": "x" * 600, "player_params.b": "y" * 400})
    found = v.validate_bundle(
        _tree(
            kit,
            Hook.PRE_SESSION,
            sized,
            outputs={"player_params.a": "{% player_params.a %}", "player_params.b": "{% player_params.b %}"},
            budget=100,
        )
    )
    assert kit.messages(found, Rule.F1) == [
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: player_params may total 1002 characters; at most 1,000"
    ]


def test_pre_session_commits_the_controls(bundle: Bundle, kit: SimpleNamespace) -> None:
    def only_mode(document: dict[str, Any]) -> None:
        root = kit.function_doc(document, "fastm_ps_root")["SequentialExecutorConfiguration"]
        del root["Output"]["aws.availSuppressionValue"]
        root["Output"]["aws.logMode"] = "{% 'DEBUG' %}"
        root["Output"]["aws.preroll"] = "{% $string(temp.x) %}"

    found = kit.messages(v.validate_bundle(kit.edit(bundle, only_mode)), Rule.F6)
    assert found == [
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: aws.availSuppressionValue is written but not re-exported, so "
        "it is discarded",
        "function fastm_ps_root Output[aws.preroll]: the root commits re-exports of its steps or literals only",
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: suppression mode and value are written together (P0-25)",
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: must commit aws.availSuppressionValue=00:00:00 (DESIGN §5.3, "
        "ADR-0021)",
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: must commit aws.logMode=DISABLED (DESIGN §5.3, ADR-0021)",
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: must commit aws.preroll=disabled (DESIGN §5.3, ADR-0021)",
    ]

    def no_output(document: dict[str, Any]) -> None:
        del kit.function_doc(document, "fastm_ps_root")["SequentialExecutorConfiguration"]["Output"]

    found = kit.messages(v.validate_bundle(kit.edit(bundle, no_output)), Rule.F6)
    assert found == [
        "PRE_SESSION_INITIALIZATION tree fastm_ps_root: PRE_SESSION maps a SEQUENTIAL_EXECUTOR whose Output commits "
        "each key"
    ]

    def direct(document: dict[str, Any]) -> None:
        for sibling in document["siblings"]:
            sibling["playback"]["FunctionMapping"]["PRE_SESSION_INITIALIZATION"] = "fastm_ps_ctl"

    found = v.validate_bundle(kit.edit(bundle, direct))
    assert (
        "PRE_SESSION_INITIALIZATION tree fastm_ps_ctl: PRE_SESSION maps a SEQUENTIAL_EXECUTOR whose Output commits "
        "each key" in kit.messages(found, Rule.F6)
    )


# --- R-F7: budgets ----------------------------------------------------------------------------------------------------


def test_request_timeouts_fit_the_executor(kit: SimpleNamespace) -> None:
    one = _http("fastm_one", request_timeout_milliseconds=500, output={"temp.a": "{% 'a' %}"})
    two = _http("fastm_two", request_timeout_milliseconds=300, output={"temp.b": "{% 'b' %}"})
    found = v.validate_bundle(_tree(kit, Hook.PRE_ADS, one, two, budget=500))
    assert kit.messages(found, Rule.F7) == [
        "function fastm_one: request timeout ≥ the executor's 500 ms: its output is lost",
        "PRE_ADS_REQUEST tree fastm_pa_root: its requests may take 800 ms; the executor allows 500 ms",
    ]


def test_hook_budgets(bundle: Bundle, kit: SimpleNamespace) -> None:
    def heavy(document: dict[str, Any]) -> None:
        kit.function_doc(document, "fastm_ps_root")["SequentialExecutorConfiguration"]["TimeoutMilliseconds"] = 2100

    found = v.validate_bundle(kit.edit(bundle, heavy))
    assert "sibling fastm-demo-p-a: the PRE_SESSION tree outlasts its 2000 ms hook" in kit.messages(found, Rule.F7)

    pa = _output("fastm_pa_x", {"temp.a": "{% 'a' %}"})
    po = _output("fastm_po_x", {"temp.b": "{% 'b' %}"})
    trees = [
        HookTree(hook=Hook.PRE_ADS, steps=(Step(function=pa),), budget_ms=1500),
        HookTree(hook=Hook.POST_ADS, steps=(Step(function=po),), budget_ms=1000),
    ]
    found = v.validate_bundle(kit.make_bundle(contributions=[kit.Contribution("t", lambda _: trees)]))
    assert "sibling fastm-demo-p-a: POST_ADS_RESPONSE may need 1000 ms; 500 ms remain" in kit.messages(found, Rule.F7)
    lone = v._Tree(Hook.POST_ADS, _http("fastm_lone", request_timeout_milliseconds=300), ())
    assert lone.timeout_ms == 300
    assert v._Tree(Hook.POST_ADS, po, ()).timeout_ms == 0


# --- the ADS request --------------------------------------------------------------------------------------------------


def test_body_templates() -> None:
    assert v.check_body_template(pb.ads_body(kids=False)) == []
    assert (
        v.check_body_template(
            '{"id":"{{session.uuid}}-{{scte.event_id}}","s":"fast-pod-{{session.avail_duration_secs}}s"}'
        )
        == []
    )
    assert v.check_body_template('{"ids":["{{session.uuid}}", 1]}') == []
    assert [x.message for x in v.check_body_template('{"a":[{{session.uuid}}]}')] == [
        "{{session.uuid}} must sit inside a JSON string"
    ]
    assert [x.message for x in v.check_body_template('{"{{session.uuid}}":"x"}')] == [
        "{{session.uuid}} must sit inside a JSON string"
    ]
    assert [x.message for x in v.check_body_template(None)] == ["a PBS sibling POSTs a body"]
    assert [x.message for x in v.check_body_template("not json")] == [
        "not valid JSON when a variable renders empty (MediaTailor skips the call)"
    ]


def test_headers(bundle: Bundle, kit: SimpleNamespace) -> None:
    def change(document: dict[str, Any]) -> None:
        playback = kit.playback_doc(document)
        headers = playback["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]
        headers.update(
            {
                "X-Fast-Origin": "secret",
                "x-fast-fv": "r0001",
                "X-Fast-Cid": "{{player_params.cid}}",
                "X-Fast-T": "{{player_params.t}}",
                "X-Unknown": "{{session.nope}}",
                "X-Fast-Probe": "1",
                "X-Fast-Pc": "other",
            }
        )
        playback["AdDecisionServerUrl"] += "&t=[player_params.t]"
        playback["LogConfiguration"]["AdsInteractionLog"]["PublishOptInEventTypes"].append("RAW_ADS_REQUEST")
        z = kit.playback_doc(document, "z")
        del z["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]["X-Fast-Probe"]

    found = kit.messages(v.validate_bundle(kit.edit(bundle, change)), Rule.P2)
    assert found == [
        "sibling fastm-demo-p-a AdDecisionServerUrl: carries ['player_params.t']; never the URL",
        "sibling fastm-demo-p-a Headers[X-Fast-Origin]: set only at apply (Secrets Manager, the release)",
        "sibling fastm-demo-p-a Headers[x-fast-fv]: set only at apply (Secrets Manager, the release)",
        "sibling fastm-demo-p-a Headers[X-Fast-Cid]: only X-Fast-T: {{player_params.t}} is player-sourced",
        "sibling fastm-demo-p-a Headers[X-Unknown]: unknown MediaTailor variables ['session.nope']",
        "sibling fastm-demo-p-a Headers: X-Fast-Pc names the sibling, fastm-demo-p-a",
        "sibling fastm-demo-p-a Headers: X-Fast-Probe is set on the probe sibling, and only there",
        "sibling fastm-demo-p-a: forwards the ticket and logs it in ['RAW_ADS_REQUEST']",
        "sibling fastm-demo-p-z Headers: X-Fast-Probe is set on the probe sibling, and only there",
    ]


def test_stored_requests(kit: SimpleNamespace) -> None:
    body = pb.ads_body(kids=False)
    assert v.check_stored_requests(body, stored_requests=kit.STORED, grid_seconds=kit.GRID) == []
    found = v.check_stored_requests(
        body, stored_requests=["fast-pod-30s", "fast-pod-60s", "fast-pod-31s", "fast-pod-300s"], grid_seconds=kit.GRID
    )
    assert [x.message for x in found] == [
        "no stored request fast-pod-90s for the 90 s breaks",
        "no stored request fast-pod-120s for the 120 s breaks",
        "fast-pod-31s exists: a 31 s ask must fail safe to the slate",
        "fast-pod-300s exists: a 300 s ask must fail safe to the slate",
    ]
    assert [
        x.message for x in v.check_stored_requests(body, stored_requests=["fast-pod-300s"], grid_seconds=[300])
    ] == ["0 s and 300 s (a missing duration) are never grid durations"]
    assert [x.message for x in v.check_stored_requests('{"id":"fixed"}', stored_requests=[], grid_seconds=[])] == [
        "the body names its stored request by session.avail_duration_secs"
    ]
    kids = pb.ads_body(kids=True)
    assert v.check_stored_requests(kids, stored_requests=kit.STORED, grid_seconds=kit.GRID) == []


def test_live_maximum_covers_the_pre_ads_hook(bundle: Bundle, kit: SimpleNamespace) -> None:
    """P0-03: a PRE_ADS hook that takes the Live max leaves no time for the ADS call, which is then never sent."""
    crawled = kit.make_bundle(contributions=[kit.crawl()])

    def tight(document: dict[str, Any]) -> None:
        timeouts = kit.playback_doc(document)["AdsPersonalizationTimeouts"]
        timeouts["LiveMaximumAdsPersonalizationTimeMilliseconds"] = 2399
        del timeouts["VodMaximumAdsPersonalizationTimeMilliseconds"]

    found = kit.messages(v.validate_bundle(kit.edit(crawled, tight)), Rule.P3)
    assert found == [
        "sibling fastm-demo-p-a: LiveMaximumAdsPersonalizationTimeMilliseconds must be at least PRE_ADS 400 ms + "
        "AdsRequestTimeout 2000 ms",
        "sibling fastm-demo-p-a: VodMaximumAdsPersonalizationTimeMilliseconds must be at least PRE_ADS 400 ms + "
        "AdsRequestTimeout 2000 ms",
    ]

    def none(document: dict[str, Any]) -> None:
        del kit.playback_doc(document)["AdsPersonalizationTimeouts"]

    assert len(kit.messages(v.validate_bundle(kit.edit(bundle, none)), Rule.P3)) == 2


def test_ad_behaviour(bundle: Bundle, kit: SimpleNamespace) -> None:
    def change(document: dict[str, Any]) -> None:
        playback = kit.playback_doc(document)
        playback["PersonalizationThresholdSeconds"] = 5
        playback["AdDecisionServerConfiguration"]["VastResponse"]["AdSequencingMode"] = "IGNORE_AD_SEQUENCE"
        playback["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]["X-Fast-Buffet"] = "0"
        playback["AvailSuppression"] = {"Mode": "OFF"}
        playback["FunctionMapping"]["PRE_MANIFEST_INSERTION"] = "fastm_ps_ctl"
        kit.playback_doc(document, "b").pop("AvailSuppression")

    found = kit.messages(v.validate_bundle(kit.edit(bundle, change)), Rule.P4)
    assert found == [
        "sibling fastm-demo-p-a: PersonalizationThresholdSeconds is 121 (slate plays unwanted slots)",
        "sibling fastm-demo-p-a: a pbs sibling uses FOLLOW_AD_SEQUENCE",
        "sibling fastm-demo-p-a: X-Fast-Buffet turns off the standalone ads FOLLOW relies on",
        "sibling fastm-demo-p-a: AvailSuppression is BEHIND_LIVE_EDGE 00:00:00 (defence in depth)",
        "sibling fastm-demo-p-a: no PRE_MANIFEST function: injection is a transcode path (P0-07)",
        "sibling fastm-demo-p-b: AvailSuppression is BEHIND_LIVE_EDGE 00:00:00 (defence in depth)",
    ]


def test_a_pbs_sibling_without_a_body(bundle: Bundle, kit: SimpleNamespace) -> None:
    def change(document: dict[str, Any]) -> None:
        del kit.playback_doc(document)["AdDecisionServerConfiguration"]["HttpRequest"]["Body"]

    found = v.validate_bundle(kit.edit(bundle, change))
    assert "sibling fastm-demo-p-a Body: a PBS sibling POSTs a body" in kit.messages(found, Rule.P1)
    assert kit.messages(found, Rule.P5) == [
        "sibling fastm-demo-p-a Body: the body names its stored request by session.avail_duration_secs"
    ]


# --- R-P6, R-C1 -------------------------------------------------------------------------------------------------------


def test_sibling_names(bundle: Bundle, kit: SimpleNamespace) -> None:
    def change(document: dict[str, Any]) -> None:
        a, b, z = document["siblings"]
        a["channel"] = "fastm-other"
        b["letter"] = "c"
        z["role"] = "general"
        z["playback"]["Name"] = "fastm-demo-p-zz"

    found = kit.messages(v.validate_bundle(kit.edit(bundle, change)), Rule.P6)
    assert found == [
        "sibling fastm-demo-p-a: 'fastm-other' is not a monetized channel of 'fastm'",
        "sibling fastm-demo-p-a: named fastm-other-p-<letter>, letter 'a'",
        "sibling fastm-demo-p-b: named fastm-demo-p-<letter>, letter 'c'",
        "sibling fastm-demo-p-b: a general sibling is one of ('a', 'b')",
        "sibling fastm-demo-p-z: a general sibling is one of ('a', 'b')",
        "sibling fastm-demo-p-z: the configuration carries the sibling's name, ≤64 characters",
        "channel fastm-demo: has the sibling pair a/b, which swap stable and idle; found ['c', 'z']",
    ]


def test_channel_policy(bundle: Bundle, kit: SimpleNamespace) -> None:
    assert v.check_channel_policy(_policy(["fastm-demo", "fastm-demo-p-?"]), channel="fastm-demo") == []
    assert v.check_channel_policy(_policy(["fastm-demo-p-?", "fastm-demo"]), channel="fastm-demo") == []
    exact = "ArnLike aws:SourceArn is exactly the playback configurations ['fastm-demo', 'fastm-demo-p-?']"
    for arns in (
        ["fastm-demo-p-?"],  # the v3.0.0 configuration is admitted too
        ["fastm-demo", "fastm-demo-p-??"],
        ["fastm-demo", "fastm-demo-p-?", "fastm-demo-p-?"],
        ["fastm-demo", "fastm-demo-p-a", "fastm-demo-p-b"],  # a per-release list
    ):
        assert [x.message for x in v.check_channel_policy(_policy(arns), channel="fastm-demo")] == [exact], arns
    single = v.check_channel_policy(_policy("arn:aws:s3:::bucket"), channel="fastm-demo")
    assert [x.message for x in single] == [exact]
    no_account = v.check_channel_policy(_policy(["fastm-demo", "fastm-demo-p-?"], account=False), channel="fastm-demo")
    assert [x.message for x in no_account] == ["conditions on aws:SourceAccount"]
    assert [x.message for x in v.check_channel_policy({"Statement": []}, channel="fastm-demo")] == [
        "no statement lets MediaTailor read the channel"
    ]

    def widened(document: dict[str, Any]) -> None:
        document["channels"][0]["policy_source_arns"] = ["fastm-demo", "fastm-demo*"]

    assert kit.messages(v.validate_bundle(kit.edit(bundle, widened)), Rule.C1) == [
        "channel fastm-demo: the policy admits the channel and <channel>-p-? only"
    ]


# --- R-S1, R-A1, R-A2 -------------------------------------------------------------------------------------------------


def test_prefetch_schedules() -> None:
    complete = {
        "Name": "b1",
        "Retrieval": {
            "DynamicVariables": {
                "scte.event_id": "820461826",
                "scte.segmentation_upid": "tears-of-steel",
                "session.avail_duration_ms": "60060",
            }
        },
    }
    assert v.check_prefetch_schedule(complete) == []
    assert v.check_prefetch_schedule({"Name": "r", "ScheduleType": "RECURRING"}) == []
    missing = v.check_prefetch_schedule({"Name": "b2", "ScheduleType": "SINGLE", "Retrieval": {}})
    assert [str(x) for x in missing] == [
        "R-S1 prefetch schedule b2: DynamicVariables must set ['scte.event_id', 'scte.segmentation_upid', "
        "'session.avail_duration_ms']"
    ]


def test_rollout_steps() -> None:
    assert v.check_rollout(ROLLOUTS[RolloutTier.LARGE]) == []
    fast = Rollout(
        name="fast",
        growth_type="EXPONENTIAL",
        growth_factor=1,
        deployment_duration_in_minutes=16,
        final_bake_time_in_minutes=10,
    )
    assert [x.message for x in v.check_rollout(fast)] == [
        "every step lasts longer than 2 minutes (an alarm needs 90-118 s)"
    ]
    preset = ROLLOUTS[RolloutTier.DEMO].model_copy(update={"name": "AppConfig.Linear50PercentEvery30Seconds"})
    assert [x.rule for x in v.check_rollout(preset)] == [Rule.A1]


def test_monitors() -> None:
    assert v.check_monitors({"pre-session-errors": "notBreaching"}) == []
    assert [x.message for x in v.check_monitors({"a": "missing", "b": None, "c": "breaching"})] == [
        "TreatMissingData is notBreaching, not missing",
        "TreatMissingData is notBreaching, not missing",
        "TreatMissingData is notBreaching, not breaching",
    ]


# --- the stored bundle and the apply ----------------------------------------------------------------------------------


def test_load_bundle(bundle: Bundle) -> None:
    document = bundle_document(bundle)
    loaded, found = v.load_bundle({**document, "digest": "0" * 64})
    assert loaded == bundle
    assert [str(x) for x in found] == ["R-F3 bundle: the stored digest does not match the content"]
    broken = json.loads(json.dumps(document))
    broken["bundle"]["functions"][0]["FunctionType"] = "HTTP_REQUEST"
    broken["bundle"]["siblings"][0]["playback"]["SlateAdUrl"] = "https://slate"
    loaded, found = v.load_bundle(broken)
    assert loaded is None
    assert {x.rule for x in found} == {Rule.F3}
    assert [x.where for x in found] == ["bundle functions.0", "bundle siblings.0.playback.SlateAdUrl"]
    assert v.load_bundle({})[0] is None


def test_rendered_requests(bundle: Bundle) -> None:
    secret, next_secret = SecretStr("value-A"), SecretStr("value-B")
    put, _ = pb.render(bundle.siblings[0].playback, prefix="fastm", release=4, change_id="c", origin_secret=secret)
    assert v.check_rendered(put, release=4, secrets=[secret, next_secret]) == []
    assert [x.message for x in v.check_rendered(put, release=5, secrets=[next_secret])] == [
        "X-Fast-Origin carries a current origin secret",
        "X-Fast-Fv is r0005",
        "PRE_SESSION_INITIALIZATION maps 'fastm_ps_root_r0004', not a FunctionId of r0005",
    ]
    leaked = json.loads(json.dumps(put))
    leaked["Tags"]["fast:change"] = "value-A"
    leaked["AdDecisionServerConfiguration"]["HttpRequest"]["Body"] = '{"x":"value-A"}'
    assert [x.message for x in v.check_rendered(leaked, release=4, secrets=[secret])] == [
        "the origin secret appears outside X-Fast-Origin"
    ]
    assert len(v.check_rendered({}, release=1, secrets=[secret])) == 2
    assert v.check_secret_free("{}", secrets=[secret], where="bundle") == []
    assert [str(x) for x in v.check_secret_free("x value-A", secrets=[secret], where="log line")] == [
        "R-P2 log line: contains the origin secret"
    ]
    assert v.check_secret_free("anything", secrets=[SecretStr("")], where="bundle") == []


def test_require_valid() -> None:
    v.require_valid([])
    problem = v.Violation(rule=Rule.F2, where="function x", message="no")
    with pytest.raises(v.InvalidBundleError, match="R-F2 function x: no") as raised:
        v.require_valid([problem])
    assert raised.value.violations == (problem,)
