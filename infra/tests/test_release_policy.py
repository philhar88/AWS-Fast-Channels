"""The release module's static channel-policy pattern (BUILD-PLAN M3-02, DESIGN §8.1, P0-08).

A monetized channel's policy admits its v3.0.0 playback configuration and the sibling pattern ``<channel>-p-?``,
set when the channel is created. Nothing else changes in the templates, and with the module off nothing changes at
all (``test_snapshots.test_shipped_templates``).
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fast_channels.core.monetization.validators import check_channel_policy

if TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

type GetDeployment = Callable[..., Any]

NEEDED_ON = {"modules.ops.enabled": True, "modules.demand.enabled": True, "modules.pds.enabled": True}
"""The modules release needs; the comparisons below start from them, so they show what release alone changes."""
RELEASE_ON = {
    **NEEDED_ON,
    "modules.release.enabled": True,
    "modules.release.channels": ["{prefix}-demo"],
    "modules.release.lab_channel": False,  # a channel of its own (test_release_stack covers it)
}
NEWS = {"channels.iac": [{"name": "{prefix}-demo"}, {"name": "{prefix}-news"}]}
ACCOUNT, REGION = "123456789012", "us-east-1"


def _unguarded(resources: dict[str, Any]) -> dict[str, Any]:
    """``resources`` without the release guard (``test_release_stack`` checks it): its managed policy, and its
    attachment to each role."""
    guards = [
        {"Ref": key}
        for key, r in resources.items()
        if key.startswith("ReleaseGuard") and r["Type"] == "AWS::IAM::ManagedPolicy"
    ]
    found = {key: r for key, r in resources.items() if {"Ref": key} not in guards}
    for key, resource in found.items():
        attached = resource.get("Properties", {}).get("ManagedPolicyArns")
        if resource["Type"] == "AWS::IAM::Role" and attached:
            kept = [a for a in attached if a not in guards]
            properties = {k: v for k, v in resource["Properties"].items() if k != "ManagedPolicyArns"}
            found[key] = resource | {"Properties": properties | ({"ManagedPolicyArns": kept} if kept else {})}
    return found


def _changed(on: Any, off: Any, stack: str) -> dict[str, Any]:
    """Resources of ``stack`` that differ between two deployments, as they are in ``on`` (without the guard)."""
    a = _unguarded(on.template(getattr(on.stage, stack)).to_json()["Resources"])
    b = off.template(getattr(off.stage, stack)).to_json()["Resources"]
    return {key: a.get(key) for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)}


def _resolved(value: Any, names: dict[str, str]) -> Any:
    """``value`` with the playback configuration and channel ARNs (``Fn::GetAtt``) and ``Fn::Join`` resolved."""
    match value:
        case {"Fn::GetAtt": [resource, "PlaybackConfigurationArn"]}:
            return f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:playbackConfiguration/{names[resource]}"
        case {"Fn::GetAtt": [resource, "Arn"]}:
            return f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:channel/{names[resource]}"
        case {"Ref": "AWS::AccountId"}:
            return ACCOUNT
        case {"Fn::Join": ["", parts]}:
            return "".join(_resolved(part, names) for part in parts)
        case dict():
            return {k: _resolved(v, names) for k, v in value.items()}
        case list():
            return [_resolved(item, names) for item in value]
    return value


def _policy(deployment: Any, channel: str) -> dict[str, Any]:
    template = deployment.template(deployment.stage.media)
    resources = template.to_json()["Resources"]
    names = {
        logical_id: r["Properties"].get("Name") or r["Properties"].get("ChannelName")
        for logical_id, r in resources.items()
        if r["Type"] in {"AWS::MediaTailor::PlaybackConfiguration", "AWS::MediaTailor::Channel"}
    }
    [policy] = [
        r["Properties"]["Policy"]
        for r in resources.values()
        if r["Type"] == "AWS::MediaTailor::ChannelPolicy" and r["Properties"]["ChannelName"] == channel
    ]
    resolved: dict[str, Any] = _resolved(policy, names)
    return resolved


def test_release_changes_only_the_policy_and_guard_of_monetized_channels(
    deployment: GetDeployment, snapshot_json: SnapshotAssertion
) -> None:
    on, off = deployment(RELEASE_ON), deployment(NEEDED_ON)

    media = _changed(on, off, "media")
    parameters = {r["Properties"]["Name"] for r in media.values() if r["Type"] == "AWS::SSM::Parameter"}
    assert parameters == {  # what the Release stack reads (M3-03), not cross-stack references
        "/fast/release/content-base-url",
        "/fast/release/cdn-base-url",
        "/fast/release/media-bucket",
        "/fast/release/channels/fast-demo",
    }
    assert {r["Type"] for r in media.values()} - {"AWS::SSM::Parameter"} == {
        "Custom::ChannelGuard",
        "AWS::MediaTailor::ChannelPolicy",
    }
    assert _changed(on, off, "data") == {}
    operator = _changed(on, off, "operator")
    [tools] = [r for r in operator.values() if r["Type"] == "AWS::Lambda::Function"]  # agent channels get the pattern
    assert {r["Type"] for r in operator.values()} == {"AWS::Lambda::Function", "AWS::SSM::Parameter"}
    [published] = [r["Properties"]["Name"] for r in operator.values() if r["Type"] == "AWS::SSM::Parameter"]
    assert published == "/fast/release/tool-functions"  # the lab channel's smoke test uses the agent tools
    assert tools["Properties"]["Handler"] == "fast_channels.functions.tools.channels.handler"
    template = tools["Properties"]["Environment"]["Variables"]["PLAYBACK_CONFIG_TEMPLATE"]["Fn::Join"][1]
    assert template[-1].endswith(',"sibling_policy":true}')

    policy = _policy(on, "fast-demo")
    assert policy["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] == [
        f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:playbackConfiguration/fast-demo",
        f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:playbackConfiguration/fast-demo-p-?",
    ]
    assert check_channel_policy(policy, channel="fast-demo") == []  # validator R-C1
    [guard] = [r for r in media.values() if r["Type"] == "Custom::ChannelGuard"]
    assert guard["Properties"]["SiblingPattern"] == "fast-demo-p-?"
    assert {"policy": policy, "guard_sibling_pattern": guard["Properties"]["SiblingPattern"]} == snapshot_json


def test_only_the_monetized_channels_admit_siblings(deployment: GetDeployment) -> None:
    on = deployment({**RELEASE_ON, **NEWS})

    assert check_channel_policy(_policy(on, "fast-demo"), channel="fast-demo") == []
    news = _policy(on, "fast-news")
    assert news["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] == (
        f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:playbackConfiguration/fast-news"
    )
    guards = on.template(on.stage.media).find_resources("Custom::ChannelGuard")
    assert {g["Properties"]["ChannelName"]: "SiblingPattern" in g["Properties"] for g in guards.values()} == {
        "fast-demo": True,
        "fast-news": False,
    }


def test_the_module_off_keeps_the_single_arn_policy(default: Any) -> None:
    policy = _policy(default, "fast-demo")

    assert policy["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] == (
        f"arn:aws:mediatailor:{REGION}:{ACCOUNT}:playbackConfiguration/fast-demo"
    )
    guards = default.template(default.stage.media).find_resources("Custom::ChannelGuard")
    assert all("SiblingPattern" not in g["Properties"] for g in guards.values())
