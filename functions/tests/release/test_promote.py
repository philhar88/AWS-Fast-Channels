"""Promotion and rollback (R1): the candidates, the pointer deployment, the swap, the remap to the last good tree, and
the deletion of trees no sibling keeps."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.stub import ANY, Stubber
from pydantic import SecretStr

from fast_channels.core.crawl.functions import CrawlContribution
from fast_channels.core.monetization.bundle import ChannelInput
from fast_channels.core.monetization.playback import render
from fast_channels.core.monetization.release import ReleaseStatus, applied
from fast_channels.functions.release import apply, promote

type Stub = Callable[[str], Stubber]
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SIBLINGS = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-z"]
HLS = "https://abc123.mediatailor.us-east-1.amazonaws.com/v1/master/0123abcd/"
CANDIDATE = {"channel": "fastm-demo", "sibling": "fastm-demo-p-b", "stable": "fastm-demo-p-a"}


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(apply, "_now", lambda: AT)
    monkeypatch.setattr(promote, "_now", lambda: AT)


def released_twice(kit: SimpleNamespace, state: Any) -> str:
    """Release 1 on every sibling, release 2 on the idle one; both bundles stored and recorded."""
    bundle = kit.make_bundle()
    key = state.add(bundle)
    first = applied(
        None, channel="fastm-demo", siblings=SIBLINGS, release=1, digest=bundle.digest, change_id="c1", at=AT
    )
    second = applied(
        first, channel="fastm-demo", siblings=["fastm-demo-p-b"], release=2, digest=bundle.digest, change_id="c2", at=AT
    )
    state.states["fastm-demo"] = (second, 2)
    for release, status in ((1, "APPLIED"), (2, "APPLIED")):
        state.statuses[release] = ReleaseStatus(
            release=release, digest=bundle.digest, bundle_key=key, change_id=f"c{release}", status=status, at=AT
        )
    state.holder = 2
    return key


def expect_deployment(appconfig: Stubber, content: dict[str, str], *, strategy: str, number: int = 7) -> None:
    appconfig.add_response(
        "list_deployments",
        {"Items": [{"DeploymentNumber": 1, "State": "COMPLETE"}, {"DeploymentNumber": 2, "State": "REVERTED"}]},
        {"ApplicationId": "app1", "EnvironmentId": "env1", "MaxResults": 5},
    )
    appconfig.add_response(
        "create_hosted_configuration_version",
        {"VersionNumber": 3},
        {
            "ApplicationId": "app1",
            "ConfigurationProfileId": "prof1",
            "Content": json.dumps(content, sort_keys=True).encode(),
            "ContentType": "application/json",
            "Description": ANY,
        },
    )
    appconfig.add_response(
        "start_deployment",
        {"DeploymentNumber": number},
        {
            "ApplicationId": "app1",
            "EnvironmentId": "env1",
            "DeploymentStrategyId": strategy,
            "ConfigurationProfileId": "prof1",
            "ConfigurationVersion": "3",
            "Description": ANY,
            "Tags": ANY,
        },
    )


def test_a_later_release_has_its_idle_sibling_as_candidate(kit: SimpleNamespace, state: Any) -> None:
    key = released_twice(kit, state)
    assert promote.candidates(key, 2, ["fastm-demo-p-b"]) == [CANDIDATE]
    first = applied(None, channel="fastm-demo", siblings=SIBLINGS, release=1, digest="d" * 64, change_id="c1", at=AT)
    state.states["fastm-demo"] = (first, 1)
    assert promote.candidates(key, 1, SIBLINGS) == []


def test_point_deploys_the_pointer_naming_the_candidate(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    released_twice(kit, state)
    expect_deployment(stub("appconfig"), {"fastm-demo": "fastm-demo-p-b"}, strategy="strat1")
    assert promote.point(2, "c2", [CANDIDATE]) == {
        "deployment": 7,
        "version": 3,
        "pointer": {"fastm-demo": "fastm-demo-p-b"},
    }
    assert ledger.actions() == [("release/bundle", "release.point")]
    stub("appconfig").add_response(
        "get_deployment",
        {"State": "ROLLED_BACK", "PercentageComplete": 10.0, "EventLog": [{"Description": "monitor in alarm"}]},
        {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 7},
    )
    assert promote.pointer_state(7) == {
        "deployment": 7,
        "state": "ROLLED_BACK",
        "percent": 10.0,
        "done": True,
        "events": ["monitor in alarm"],
    }


def test_promote_swaps_the_pair_rewrites_the_plan_and_gives_the_lock_back(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    key = released_twice(kit, state)
    stub("mediatailor").add_response(
        "get_playback_configuration", {"HlsConfiguration": {"ManifestEndpointPrefix": HLS}}, {"Name": "fastm-demo-p-b"}
    )
    stub("s3").add_response(
        "put_object",
        {},
        {
            "Bucket": "fastm-media",
            "Key": "plan/playback/fastm-demo.json",
            "Body": ANY,
            "ContentType": "application/json",
            "CacheControl": "max-age=30",
        },
    )
    assert promote.promote(key, 2, "c2", [CANDIDATE]) == {"release": 2, "stable": {"fastm-demo": "fastm-demo-p-b"}}
    channel, version = state.states["fastm-demo"]
    assert (channel.stable, channel.idle, version) == ("fastm-demo-p-b", "fastm-demo-p-a", 3)
    assert state.statuses[2].status == "PROMOTED" and state.holder is None
    assert state.deployed == {"fastm-demo": "fastm-demo-p-b"}
    assert ledger.actions() == [("release/fastm-demo", "release.promote")]


def test_rollback_remaps_the_candidate_to_its_last_good_tree(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    key = released_twice(kit, state)
    bundle = state.bundle(key)
    sibling = bundle.siblings[1]
    put, _ = render(
        sibling.playback, prefix="fastm", release=1, change_id="c1", origin_secret=SecretStr("secret-value-a")
    )
    mediatailor = stub("mediatailor")
    mediatailor.add_response("put_playback_configuration", {}, put)  # back on fastm_ps_root_r0001
    mediatailor.add_response(
        "get_playback_configuration", {"HlsConfiguration": {"ManifestEndpointPrefix": HLS}}, {"Name": "fastm-demo-p-a"}
    )
    stub("s3").add_response("put_object", {}, None)
    stub("appconfig").add_response("list_deployments", {"Items": []}, None)  # nothing to stop
    expect_deployment(stub("appconfig"), {"fastm-demo": "fastm-demo-p-a"}, strategy="now1", number=8)
    result = promote.rollback(key, 2, "c2", [CANDIDATE], {"Error": "SmokeFailed"})
    assert result == {"release": 2, "remapped": {"fastm-demo-p-b": 1}, "deployment": 8}
    channel, _ = state.states["fastm-demo"]
    assert channel.siblings["fastm-demo-p-b"].release == 1 and channel.history["fastm-demo-p-b"] == (1,)
    assert (channel.stable, channel.idle) == ("fastm-demo-p-a", "fastm-demo-p-b")
    assert state.statuses[2].status == "ROLLED_BACK" and state.holder is None
    assert ledger.actions() == [("release/bundle", "release.rollback")]
    assert ledger.entries[0].detail["reason"] == {"Error": "SmokeFailed"}


def test_finish_publishes_a_changed_pointer_at_once_and_deletes_old_trees(
    kit: SimpleNamespace, state: Any, stub: Stub
) -> None:
    bundle = kit.make_bundle()
    state.add(bundle)
    first = applied(
        None, channel="fastm-demo", siblings=SIBLINGS, release=4, digest=bundle.digest, change_id="c4", at=AT
    )
    state.states["fastm-demo"] = (first, 1)
    state.holder = 4
    expect_deployment(stub("appconfig"), {"fastm-demo": "fastm-demo-p-a"}, strategy="now1")
    owned = {"fast:app": "fastm", "fast:owner": "release-controller"}
    stub("mediatailor").add_response(
        "list_functions",
        {
            "Items": [
                {"FunctionId": "fastm_ps_ctl_r0001", "FunctionType": "CUSTOM_OUTPUT", "Tags": owned},
                {"FunctionId": "fastm_ps_root_r0001", "FunctionType": "SEQUENTIAL_EXECUTOR", "Tags": owned},
                {"FunctionId": "fastm_ps_root_r0004", "FunctionType": "SEQUENTIAL_EXECUTOR", "Tags": owned},
            ]
        },
    )
    stub("mediatailor").add_response("delete_function", {}, {"FunctionId": "fastm_ps_root_r0001"})
    stub("mediatailor").add_response("delete_function", {}, {"FunctionId": "fastm_ps_ctl_r0001"})
    result = promote.finish(4, "c4")
    assert result["deleted"] == ["fastm_ps_root_r0001", "fastm_ps_ctl_r0001"] and state.holder is None
    assert state.deployed == {"fastm-demo": "fastm-demo-p-a"}
    state.holder = 4
    stub("mediatailor").add_response("list_functions", {"Items": []})
    assert promote.finish(4, "c4")["deployment"] is None  # unchanged: nothing deployed


def test_candidates_are_smoke_tested_where_a_channel_runs(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub, context: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = released_twice(kit, state)
    event = {"step": "candidates", "bundle_key": key, "release": 2, "change_id": "c2", "siblings": ["fastm-demo-p-b"]}
    mediatailor = stub("mediatailor")
    mediatailor.add_response(
        "describe_channel", {"ChannelState": "RUNNING", "LogConfiguration": {}}, {"ChannelName": "fastm-demo"}
    )
    assert promote.handler(event, context) == {"candidates": [CANDIDATE], "smoke": [CANDIDATE], "lab": None}
    mediatailor.add_response(
        "describe_channel", {"ChannelState": "STOPPED", "LogConfiguration": {}}, {"ChannelName": "fastm-demo"}
    )
    assert promote.handler(event, context) == {"candidates": [CANDIDATE], "smoke": [], "lab": None}
    assert ledger.actions() == [("release/bundle", "release.hold")]  # no lab channel: applied, not promoted
    mediatailor.add_client_error("describe_channel", service_error_code="NotFoundException", http_status_code=404)
    assert not promote.on_air("fastm-demo")
    with pytest.raises(ValueError, match="unknown step 'x'"):
        promote.handler({"step": "x"}, context)


def test_the_lab_channel_is_smoke_tested_when_no_monetized_channel_runs(
    kit: SimpleNamespace, state: Any, stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    channels = [
        ChannelInput(name="fastm-demo", video_content_source_url=kit.CA_URL),
        ChannelInput(name="fastm-lab", video_content_source_url=kit.CA_URL.replace("demo", "lab"), lab=True),
    ]
    key = state.add(kit.make_bundle(channels=channels))
    lab = {"channel": "fastm-lab", "sibling": "fastm-lab-p-b", "stable": "fastm-lab-p-a"}
    stub("mediatailor").add_response(
        "describe_channel", {"ChannelState": "STOPPED", "LogConfiguration": {}}, {"ChannelName": "fastm-demo"}
    )
    monkeypatch.setenv("TOOL_FUNCTIONS", '{"channels": "f1"}')
    promote.settings.cache_clear()
    assert promote.smoke_plan(key, [CANDIDATE, lab]) == {"smoke": [lab], "lab": "fastm-lab"}


def test_runbook_r1_remaps_a_pair_sibling_in_alarm_to_its_last_good_tree(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub, context: Any
) -> None:
    key = released_twice(kit, state)
    state.holder = None  # no release applying
    bundle = state.bundle(key)
    put, _ = render(
        bundle.siblings[1].playback,
        prefix="fastm",
        release=1,
        change_id="c1",
        origin_secret=SecretStr("secret-value-a"),
    )
    stub("cloudwatch").add_response(
        "describe_alarms",
        {
            "MetricAlarms": [
                {"AlarmName": "fastm-demo-p-b-presessioninithook-errors"},
                {"AlarmName": "fastm-demo-p-a-getmanifest-errors"},  # not the tree's: no remap
                {"AlarmName": "other"},
            ]
        },
        {"StateValue": "ALARM", "AlarmTypes": ["MetricAlarm"]},
    )
    mediatailor = stub("mediatailor")
    mediatailor.add_response("put_playback_configuration", {}, put)
    mediatailor.add_response(
        "get_playback_configuration", {"HlsConfiguration": {"ManifestEndpointPrefix": HLS}}, {"Name": "fastm-demo-p-a"}
    )
    stub("s3").add_response("put_object", {}, None)
    alarm = {
        "id": "event-1",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {"alarmName": "fastm-release-candidate", "state": {"value": "ALARM"}},
    }
    assert promote.handler(alarm, context) == {"remapped": {"fastm-demo-p-b": 1}}
    channel, _ = state.states["fastm-demo"]
    assert channel.siblings["fastm-demo-p-b"].release == 1 and channel.history["fastm-demo-p-b"] == (1,)
    assert ledger.actions() == [("release/fastm-demo", "release.rollback")]
    assert ledger.entries[0].detail == {
        "runbook": "R1",
        "alarm": "fastm-release-candidate",
        "sibling": "fastm-demo-p-b",
        "release": 1,
    }


def test_runbook_r1_leaves_a_release_in_progress_to_its_own_rollback(
    kit: SimpleNamespace, state: Any, ledger: Any
) -> None:
    released_twice(kit, state)  # release 2 holds the lock
    assert promote.runbook("fastm-release-candidate", "event-2") == {"remapped": {}, "deferred_to": 2}
    assert ledger.entries == []


def test_runbook_r1_skips_a_sibling_without_a_last_good_tree(
    kit: SimpleNamespace, state: Any, ledger: Any, stub: Stub
) -> None:
    bundle = kit.make_bundle()
    state.add(bundle)
    first = applied(
        None, channel="fastm-demo", siblings=SIBLINGS, release=1, digest=bundle.digest, change_id="c1", at=AT
    )
    state.states["fastm-demo"] = (first, 1)
    stub("cloudwatch").add_response(
        "describe_alarms", {"MetricAlarms": [{"AlarmName": "fastm-demo-p-a-function-errors"}]}, None
    )
    assert promote.runbook("fastm-release-candidate", "event-3") == {"remapped": {}}
    assert ledger.entries == []


def test_a_pointer_waits_while_another_deployment_of_the_environment_runs(stub: Stub) -> None:
    stub("appconfig").add_response("list_deployments", {"Items": [{"DeploymentNumber": 4, "State": "BAKING"}]}, None)
    with pytest.raises(promote.PointerBusyError):
        promote._deploy({"fastm-demo": "fastm-demo-p-a"}, strategy="now1", description="d", tags={})


def test_a_rollback_stops_the_pointers_rollout_but_not_the_flags(stub: Stub) -> None:
    appconfig = stub("appconfig")
    appconfig.add_response(
        "list_deployments",
        {"Items": [{"DeploymentNumber": 11, "State": "DEPLOYING"}, {"DeploymentNumber": 10, "State": "BAKING"}]},
        {"ApplicationId": "app1", "EnvironmentId": "env1", "MaxResults": 5},
    )
    appconfig.add_response(
        "get_deployment",
        {"DeploymentNumber": 11, "ConfigurationProfileId": "prof1", "State": "DEPLOYING"},
        {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 11},
    )
    appconfig.add_response(
        "stop_deployment", {}, {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 11}
    )
    appconfig.add_response(
        "get_deployment",
        {"DeploymentNumber": 10, "ConfigurationProfileId": "flags1", "State": "BAKING"},
        {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 10},
    )
    assert promote.stop_pointer() == [11]


def expect_latency(cloudwatch: Stubber, function_id: str, maxima: list[float]) -> None:
    cloudwatch.add_response(
        "get_metric_statistics",
        {"Datapoints": [{"Maximum": m} for m in maxima]},
        {
            "Namespace": "AWS/MediaTailor",
            "MetricName": "Function.Latency",
            "Dimensions": [
                {"Name": "ConfigurationName", "Value": "fastm-demo-p-b"},
                {"Name": "FunctionId", "Value": function_id},
                {"Name": "HookType", "Value": "PRE_ADS_REQUEST"},
                {"Name": "FunctionType", "Value": "HTTP_REQUEST"},
            ],
            "StartTime": AT,
            "EndTime": AT,
            "Period": 60,
            "Statistics": ["Maximum"],
        },
    )


def test_a_candidates_request_at_its_timeout_stops_the_pointer(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    key = state.add(kit.make_bundle(contributions=[CrawlContribution()]))
    appconfig, cloudwatch = stub("appconfig"), stub("cloudwatch")
    where = {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 7}
    appconfig.add_response("get_deployment", {"State": "DEPLOYING", "PercentageComplete": 20.0, "StartedAt": AT}, where)
    expect_latency(cloudwatch, "fastm_pa_meta_r0002", [266.0, 255.0, 30.0])  # no error metric counts these
    appconfig.add_response("stop_deployment", {}, where)
    found = promote.pointer_state(7, key, 2, [CANDIDATE])
    assert (found["state"], found["done"]) == ("ROLLED_BACK", True)
    assert found["timed_out"] == {"fastm-demo-p-b": {"fastm_pa_meta_r0002": {"timeout_ms": 250, "minutes": 2}}}


def test_a_request_that_timed_out_once_leaves_the_pointer_running(kit: SimpleNamespace, state: Any, stub: Stub) -> None:
    key = state.add(kit.make_bundle(contributions=[CrawlContribution()]))
    where = {"ApplicationId": "app1", "EnvironmentId": "env1", "DeploymentNumber": 7}
    stub("appconfig").add_response(
        "get_deployment", {"State": "DEPLOYING", "PercentageComplete": 20.0, "StartedAt": AT}, where
    )
    expect_latency(stub("cloudwatch"), "fastm_pa_meta_r0002", [266.0, 77.0])  # a cold first call
    found = promote.pointer_state(7, key, 2, [CANDIDATE])
    assert (found["state"], found["done"], "timed_out" in found) == ("DEPLOYING", False, False)
