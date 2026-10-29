"""The drift check: each recorded sibling, its Function tree and the channel's policy against what was applied;
drift is reported by path, never by value."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from fast_channels.core import specs
from fast_channels.core.monetization.playback import render
from fast_channels.core.monetization.release import applied
from fast_channels.functions.release import drift

type Stub = Callable[[str], Stubber]
AT = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SECRET = SecretStr("secret-value-a")
ACCOUNT = "123456789012"
CHANNEL_ARN = f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:channel/fastm-demo"
PC_ARN = f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:playbackConfiguration/fastm-demo"


def live(sibling: Any, *, release: int = 2, change: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """``GetPlaybackConfiguration`` of a sibling as release ``release`` left it (and ``change`` then made it)."""
    put, logging = render(sibling.playback, prefix="fastm", release=release, change_id="c2", origin_secret=SECRET)
    response = put | {
        "PlaybackConfigurationArn": f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:playbackConfiguration/{sibling.name}",
        "LogConfiguration": {k: v for k, v in logging.items() if k != "PlaybackConfigurationName"},
    }
    if change:
        change(response)
    return response


def expect_tree(mediatailor: Stubber, bundle: Any, *, release: int = 2, missing: str | None = None) -> None:
    root = bundle.function("fastm_ps_root")
    for function in (root, bundle.function("fastm_ps_ctl")):
        released = function.released(release)
        if released.function_id == missing:
            mediatailor.add_client_error("get_function", service_error_code="NotFoundException", http_status_code=404)
            continue
        mediatailor.add_response("get_function", released.put_request(tags=False), {"FunctionId": released.function_id})


def test_a_sibling_as_released_has_no_drift(kit: SimpleNamespace, stub: Stub) -> None:
    bundle = kit.make_bundle()
    sibling = bundle.siblings[0]
    mediatailor = stub("mediatailor")
    mediatailor.add_response("get_playback_configuration", live(sibling), {"Name": sibling.name})
    expect_tree(mediatailor, bundle)
    assert drift.sibling_drift(sibling, bundle, release=2, change_id="c2", secret=SECRET) == []


def test_drift_names_the_paths_never_the_values(kit: SimpleNamespace, stub: Stub) -> None:
    bundle = kit.make_bundle()
    sibling = bundle.siblings[1]

    def edit(response: dict[str, Any]) -> None:
        response["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]["X-Fast-Origin"] = "someone-elses"
        response["PersonalizationThresholdSeconds"] = 2
        response["LogConfiguration"]["PercentEnabled"] = 10

    mediatailor = stub("mediatailor")
    mediatailor.add_response("get_playback_configuration", live(sibling, change=edit), {"Name": sibling.name})
    expect_tree(mediatailor, bundle, missing="fastm_ps_ctl_r0002")
    found = drift.sibling_drift(sibling, bundle, release=2, change_id="c2", secret=SECRET)
    assert found == [
        "fastm-demo-p-b: AdDecisionServerConfiguration.HttpRequest.Headers.X-Fast-Origin",
        "fastm-demo-p-b: PersonalizationThresholdSeconds",
        "fastm-demo-p-b: LogConfiguration.PercentEnabled",
        "fastm_ps_ctl_r0002: missing",
    ]
    assert "secret-value" not in json.dumps(found) and "someone-elses" not in json.dumps(found)


def test_a_deleted_sibling_is_drift(kit: SimpleNamespace, stub: Stub) -> None:
    bundle = kit.make_bundle()
    stub("mediatailor").add_client_error(
        "get_playback_configuration", service_error_code="NotFoundException", http_status_code=404
    )
    assert drift.sibling_drift(bundle.siblings[2], bundle, release=2, change_id="c2", secret=SECRET) == [
        "fastm-demo-p-z: missing"
    ]
    stub("mediatailor").add_client_error("get_playback_configuration", service_error_code="AccessDeniedException")
    with pytest.raises(drift.ClientError):
        drift.sibling_drift(bundle.siblings[2], bundle, release=2, change_id="c2", secret=SECRET)


def test_policy_drift_checks_rule_c1(stub: Stub) -> None:
    mediatailor = stub("mediatailor")
    good = specs.channel_policy(
        channel_arn=CHANNEL_ARN, playback_configuration_arn=PC_ARN, account=ACCOUNT, siblings=True
    )
    mediatailor.add_response("get_channel_policy", {"Policy": json.dumps(good)}, {"ChannelName": "fastm-demo"})
    assert drift.policy_drift("fastm-demo") == []
    narrow = specs.channel_policy(channel_arn=CHANNEL_ARN, playback_configuration_arn=PC_ARN, account=ACCOUNT)
    mediatailor.add_response("get_channel_policy", {"Policy": json.dumps(narrow)}, {"ChannelName": "fastm-demo"})
    assert drift.policy_drift("fastm-demo") == [
        "fastm-demo: ArnLike aws:SourceArn is exactly the playback configurations ['fastm-demo', 'fastm-demo-p-?']"
    ]
    mediatailor.add_client_error("get_channel_policy", service_error_code="NotFoundException", http_status_code=404)
    assert drift.policy_drift("fastm-demo") == ["fastm-demo: no channel policy"]


def test_states_reads_every_channel_page(stub: Stub) -> None:
    state = applied(
        None, channel="fastm-demo", siblings=["fastm-demo-p-a"], release=1, digest="a" * 64, change_id="c", at=AT
    )
    removed = state.model_copy(update={"channel": "fastm-news"})  # taken out of the module: not checked
    dynamodb = stub("dynamodb")
    item = {"pk": {"S": "CHANNEL#fastm-demo"}, "doc": {"S": state.model_dump_json()}}
    dynamodb.add_response("scan", {"Items": [item], "LastEvaluatedKey": {"pk": {"S": "CHANNEL#fastm-demo"}}})
    dynamodb.add_response(
        "scan", {"Items": [{"pk": {"S": "CHANNEL#fastm-news"}, "doc": {"S": removed.model_dump_json()}}]}
    )
    assert drift.states() == [state]


def test_the_handler_publishes_the_drift_count(
    kit: SimpleNamespace, state: Any, monkeypatch: pytest.MonkeyPatch, context: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = kit.make_bundle()
    state.add(bundle)
    siblings = ["fastm-demo-p-a", "fastm-demo-p-b", "fastm-demo-p-x"]  # x: recorded, no longer in the bundle
    recorded = applied(
        None, channel="fastm-demo", siblings=siblings, release=1, digest=bundle.digest, change_id="c1", at=AT
    )
    checked: list[tuple[str, int, str, str]] = []

    def sibling_drift(sibling: Any, _: Any, *, release: int, change_id: str, secret: SecretStr) -> list[str]:
        checked.append((sibling.name, release, change_id, secret.get_secret_value()))
        return ["fastm-demo-p-b: Tags.fast:release"] if sibling.name.endswith("b") else []

    monkeypatch.setattr(drift, "states", lambda: [recorded])
    monkeypatch.setattr(drift, "policy_drift", lambda channel: [])
    monkeypatch.setattr(drift, "sibling_drift", sibling_drift)
    result = drift.handler({"scheduled_time": "2026-10-29T12:00:00Z"}, context)
    assert result == {
        "siblings": 2,
        "drift": ["fastm-demo-p-b: Tags.fast:release", "fastm-demo-p-x: not in the bundle of r0001"],
    }
    assert checked == [("fastm-demo-p-a", 1, "c1", "secret-value-a"), ("fastm-demo-p-b", 1, "c1", "secret-value-a")]
    metric = next(json.loads(line) for line in capsys.readouterr().out.splitlines() if '"ReleaseDrift"' in line)
    assert metric["ReleaseDrift"] == [2.0] or metric["ReleaseDrift"] == 2.0
    assert "secret-value" not in json.dumps(metric)
