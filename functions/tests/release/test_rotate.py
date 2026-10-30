"""Origin-secret rotation: the lock, the WAF's two values, the siblings' switch, the verification and the retirement."""

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from fast_channels.core.monetization import names
from fast_channels.core.monetization.release import ReleaseStatus, applied
from fast_channels.functions.release import rotate, store

type Stub = Callable[[str], Stubber]
AT = datetime(2026, 10, 29, 12, tzinfo=UTC)
CHANNEL = "fastm-demo"
SECRET_A, SECRET_B = "secret-value-a", "secret-value-b"
SIBLINGS = [f"{CHANNEL}-p-a", f"{CHANNEL}-p-b", f"{CHANNEL}-p-z"]
GROUP = {"Name": "fastm-demand-origin", "Id": "g1", "ARN": "arn:aws:wafv2:us-east-1:123456789012:global/rulegroup/x/g1"}


def rule_group(a: bytes, b: bytes) -> dict[str, Any]:
    def match(value: bytes) -> dict[str, Any]:
        return {
            "ByteMatchStatement": {
                "SearchString": value,
                "FieldToMatch": {"SingleHeader": {"Name": "x-fast-origin"}},
                "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
                "PositionalConstraint": "EXACTLY",
            }
        }

    path = {
        "ByteMatchStatement": {
            "SearchString": b"/openrtb2/",
            "FieldToMatch": {"UriPath": {}},
            "TextTransformations": [{"Priority": 0, "Type": "NONE"}],
            "PositionalConstraint": "STARTS_WITH",
        }
    }
    visibility = {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "m"}
    origin = {
        "Name": "fast-origin-required",
        "Priority": 0,
        "Action": {"Block": {}},
        "VisibilityConfig": visibility,
        "Statement": {
            "AndStatement": {
                "Statements": [
                    path,
                    {"NotStatement": {"Statement": {"OrStatement": {"Statements": [match(a), match(b)]}}}},
                ]
            }
        },
    }
    paths = {
        "Name": "fast-paths",
        "Priority": 1,
        "Action": {"Block": {}},
        "VisibilityConfig": visibility,
        "Statement": path,
    }
    return {**GROUP, "Capacity": 50, "Description": "d", "VisibilityConfig": visibility, "Rules": [origin, paths]}


@pytest.fixture
def released(state: Any, kit: SimpleNamespace) -> SimpleNamespace:
    """fastm-demo released once (r0001 on its three siblings)."""
    bundle = kit.make_bundle()
    key = state.add(bundle)
    state.statuses[1] = ReleaseStatus(
        release=1, digest=bundle.digest, bundle_key=key, change_id="c1", status="APPLIED", at=AT
    )
    first = applied(None, channel=CHANNEL, siblings=SIBLINGS, release=1, digest=bundle.digest, change_id="c1", at=AT)
    state.states[CHANNEL] = (first, 1)
    return SimpleNamespace(bundle=bundle, key=key)


def test_begin_holds_the_lock_and_chooses_the_other_slot(state: Any, ledger: Any) -> None:
    assert rotate.begin("x1") == {"current": "a", "target": "b"}
    assert state.holder == rotate.ROTATION
    assert ledger.actions() == [(names.ROTATION_CHAIN, "rotation.begin")]
    state.holder = 7
    with pytest.raises(store.ReleaseBusyError):
        rotate.begin("x2")


def test_the_waf_admits_exactly_the_two_values(stub: Stub) -> None:
    waf = stub("wafv2")
    waf.add_response("list_rule_groups", {"RuleGroups": [GROUP], "NextMarker": "m"}, {"Scope": "CLOUDFRONT"})
    before = rule_group(b"old-a", b"old-b")
    waf.add_response(
        "get_rule_group",
        {"RuleGroup": before, "LockToken": "t1"},
        {"Name": GROUP["Name"], "Scope": "CLOUDFRONT", "Id": "g1"},
    )
    after = copy.deepcopy(before["Rules"])
    statements = after[0]["Statement"]["AndStatement"]["Statements"][1]["NotStatement"]["Statement"]["OrStatement"]
    statements["Statements"][0]["ByteMatchStatement"]["SearchString"] = b"new-a"
    statements["Statements"][1]["ByteMatchStatement"]["SearchString"] = b"old-b"
    waf.add_response(
        "update_rule_group",
        {"NextLockToken": "t2"},
        {
            "Name": GROUP["Name"],
            "Scope": "CLOUDFRONT",
            "Id": "g1",
            "Description": "d",
            "Rules": after,
            "VisibilityConfig": before["VisibilityConfig"],
            "LockToken": "t1",
        },
    )
    rotate.admit_values({"a": SecretStr("new-a"), "b": SecretStr("old-b")})


def test_a_rule_that_does_not_match_the_origin_header_is_refused(stub: Stub) -> None:
    waf = stub("wafv2")
    waf.add_response("list_rule_groups", {"RuleGroups": [GROUP]}, None)
    group = rule_group(b"a", b"b")
    group["Rules"][0]["Statement"]["AndStatement"]["Statements"][1]["NotStatement"]["Statement"]["OrStatement"][
        "Statements"
    ][1]["ByteMatchStatement"]["FieldToMatch"]["SingleHeader"]["Name"] = "x-other"
    waf.add_response("get_rule_group", {"RuleGroup": group, "LockToken": "t1"}, None)
    with pytest.raises(ValueError, match="not x-fast-origin"):
        rotate.admit_values({"a": SecretStr("a"), "b": SecretStr("b")})


def test_admit_gives_the_other_slot_a_new_value(
    stub: Stub, state: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = stub("secretsmanager")
    secrets.add_response(
        "get_random_password",
        {"RandomPassword": "fresh"},
        {"PasswordLength": 48, "ExcludePunctuation": True, "IncludeSpace": False},
    )
    secrets.add_response("put_secret_value", {}, {"SecretId": "fastm/demand/origin-b", "SecretString": "fresh"})
    admitted: list[dict[str, str]] = []
    monkeypatch.setattr(
        rotate, "admit_values", lambda values: admitted.append({k: v.get_secret_value() for k, v in values.items()})
    )
    assert rotate.admit("b", "x1") == {"admitted": "b"}
    assert admitted == [{"a": SECRET_A, "b": SECRET_B}]  # the fake store's values; a real one re-reads b
    assert ledger.entries[0].detail == {"slot": "b"}


@pytest.mark.parametrize(("status", "admitted"), [(200, True), (400, True), (403, False)])
def test_the_probe_passes_unless_the_waf_blocks_it(
    state: Any, monkeypatch: pytest.MonkeyPatch, status: int, admitted: bool
) -> None:
    monkeypatch.setenv("PBS_ORIGIN", "https://d222222abcdef8.cloudfront.net")
    rotate.settings.cache_clear()
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    original = httpx.post
    monkeypatch.setattr(
        rotate.httpx, "post", lambda url, **kw: httpx.Client(transport=httpx.MockTransport(answer)).post(url, **kw)
    )
    del original
    if admitted:
        assert rotate.probe("b") == {"status": status}
    else:
        with pytest.raises(rotate.NotAdmittedError):
            rotate.probe("b")
    assert seen[0].headers["X-Fast-Origin"] == SECRET_B and seen[0].headers["X-Fast-Probe"] == "1"


def test_switch_puts_every_configured_sibling_on_the_new_value(
    stub: Stub, state: Any, ledger: Any, released: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots: list[str] = []
    monkeypatch.setattr(store, "put_origin_slot", slots.append)
    mediatailor = stub("mediatailor")
    for _ in SIBLINGS:
        mediatailor.add_response("put_playback_configuration", {}, None)
    result = rotate.switch("b", "x1")
    assert result["switched"] == SIBLINGS and slots == ["b"]
    assert ledger.entries[0].detail == {"slot": "b", "siblings": SIBLINGS}
    assert SECRET_B not in str(ledger.entries[0].detail) and SECRET_A not in str(ledger.entries[0].detail)


def test_switch_sends_the_new_value_in_each_sibling_of_its_own_release(
    stub: Stub, state: Any, ledger: Any, released: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "put_origin_slot", lambda slot: None)
    puts: list[dict[str, Any]] = []
    monkeypatch.setattr(
        rotate.aws, "client", lambda service: SimpleNamespace(put_playback_configuration=lambda **kw: puts.append(kw))
    )
    rotate.switch("b", "x1")
    for put in puts:
        headers = put["AdDecisionServerConfiguration"]["HttpRequest"]["Headers"]
        assert headers["X-Fast-Origin"] == SECRET_B and headers["X-Fast-Fv"] == "r0001"
        assert put["Tags"]["fast:change"] == "c1", "the release's own change tag: no drift"


@pytest.mark.parametrize(
    ("counts", "outcome"),
    [
        ({"MAKING_ADS_REQUEST": 3, "FILLED_AVAIL": 3}, "verified"),
        ({"MAKING_ADS_REQUEST": 3}, rotate.NotVerifiedError),
        ({}, rotate.NotVerifiedError),
        ({"MAKING_ADS_REQUEST": 3, "FILLED_AVAIL": 2, "ERROR_ADS_INVALID_RESPONSE": 1}, rotate.RefusedError),
    ],
)
def test_verify_needs_an_answered_break_while_a_channel_is_on_air(
    monkeypatch: pytest.MonkeyPatch, counts: dict[str, int], outcome: Any
) -> None:
    monkeypatch.setattr(rotate.promote, "on_air", lambda channel: True)
    rows = [{"eventType": k, "n": str(v)} for k, v in counts.items()]
    monkeypatch.setattr(rotate.smoke, "insights", lambda group, query, start, end: rows)
    if outcome == "verified":
        assert rotate.verify(SIBLINGS, 0.0)["verified"]
    else:
        with pytest.raises(outcome):
            rotate.verify(SIBLINGS, 0.0)


def test_verify_without_a_channel_on_air_rests_on_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rotate.promote, "on_air", lambda channel: False)
    assert rotate.verify(SIBLINGS, 0.0)["verified"] is False


def test_retire_and_fail_give_the_lock_back(state: Any, ledger: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rotate, "_new_value", lambda slot: None)
    monkeypatch.setattr(rotate, "admit_values", lambda values: None)
    state.holder = rotate.ROTATION
    assert rotate.retire("a", "x1") == {"retired": "a"} and state.holder is None
    state.holder = rotate.ROTATION
    assert rotate.fail("x1", {"Error": "NotAdmittedError", "Cause": "slot b is not admitted yet"}) == {"failed": True}
    assert state.holder is None
    assert ledger.actions() == [(names.ROTATION_CHAIN, "rotation.retire"), (names.ROTATION_CHAIN, "rotation.fail")]


def test_the_handler_dispatches(monkeypatch: pytest.MonkeyPatch, context: Any) -> None:
    monkeypatch.setitem(rotate.STEPS, "begin", lambda event: {"step": "begin"})
    assert rotate.handler({"step": "begin", "change_id": "x"}, context) == {"step": "begin"}
    with pytest.raises(ValueError, match="unknown step"):
        rotate.handler({"step": "x"}, context)
