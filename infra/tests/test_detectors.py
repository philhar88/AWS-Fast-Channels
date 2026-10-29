"""The ops module's alarms and detectors: account quota alarms, hook alarms, the zero-ad detector, the alerts poller."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.infra.constructs.ops.detectors import HOOK_METRICS

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

type GetDeployment = Callable[..., Any]


def resources(template: Template, kind: str) -> dict[str, Any]:
    found: dict[str, Any] = template.find_resources(kind)
    return found


@pytest.fixture(scope="module")
def ops(deployment: GetDeployment) -> Template:
    d = deployment({"modules.ops.enabled": True})
    template: Template = d.template(d.stage.ops)
    return template


def alarms(template: Template) -> dict[str, dict[str, Any]]:
    return {
        r["Properties"]["AlarmName"]: r["Properties"] for r in resources(template, "AWS::CloudWatch::Alarm").values()
    }


def test_account_quota_alarms_sum_the_whole_account_at_80_percent(ops: Template) -> None:
    found = alarms(ops)
    requests, channels = found["fast-account-ad-insertion-requests"], found["fast-account-channel-assembly-requests"]

    assert [m["Expression"] for m in requests["Metrics"]] == [
        'SELECT SUM(Requests) FROM SCHEMA("AWS/MediaTailor", ConfigurationName)'
    ]
    assert requests["Metrics"][0]["Period"] == 60
    assert requests["Threshold"] == 0.8 * 10_000 * 60
    assert [m["Expression"] for m in channels["Metrics"]] == [
        'SELECT SUM(RequestCount) FROM SCHEMA("AWS/MediaTailor", Channel)'
    ]
    assert channels["Threshold"] == 0.8 * 400 * 60
    for alarm in (requests, channels):
        assert (alarm["EvaluationPeriods"], alarm["TreatMissingData"]) == (3, "notBreaching")
        assert alarm["AlarmActions"] and alarm["OKActions"]


def test_every_iac_configuration_alarms_on_hook_and_manifest_errors(deployment: GetDeployment) -> None:
    d = deployment({"modules.ops.enabled": True, "channels.iac": [{"name": "fast-demo"}, {"name": "fast-news"}]})
    found = alarms(d.template(d.stage.ops))
    hook_alarms = {name: a for name, a in found.items() if a.get("Namespace") == "AWS/MediaTailor"}

    assert len(hook_alarms) == 2 * len(HOOK_METRICS)
    assert hook_alarms["fast-news-presessioninithook-errors"]["Dimensions"] == [
        {"Name": "ConfigurationName", "Value": "fast-news"}
    ]
    assert {a["MetricName"] for a in hook_alarms.values()} == set(HOOK_METRICS)
    assert all(a["Threshold"] == 1 and a["TreatMissingData"] == "notBreaching" for a in hook_alarms.values())


def _function(template: Template, handler: str) -> dict[str, Any]:
    [function] = [
        r["Properties"]
        for r in resources(template, "AWS::Lambda::Function").values()
        if r["Properties"]["Handler"] == handler
    ]
    return function


def _schedule(template: Template, words: str) -> dict[str, Any]:
    [schedule] = [
        r["Properties"]
        for r in resources(template, "AWS::Scheduler::Schedule").values()
        if words in r["Properties"].get("Description", "")
    ]
    return schedule


def test_the_zero_ads_detector_runs_hourly_on_the_vended_logs(ops: Template) -> None:
    function = _function(ops, "fast_channels.functions.ops.zero_ads_sessions.handler")
    schedule = _schedule(ops, "zero-ad sessions")
    alarm = alarms(ops)["fast-zero-ads-sessions"]
    policies = json.dumps(resources(ops, "AWS::IAM::Policy"))

    assert function["Environment"]["Variables"]["ADS_LOG_GROUP"] == "/aws/vendedlogs/mediatailor/fast"
    assert schedule["ScheduleExpression"] == "rate(1 hour)"
    assert json.loads(schedule["Target"]["Input"]) == {"scheduled_time": "<aws.scheduler.scheduled-time>"}
    assert (alarm["MetricName"], alarm["Namespace"], alarm["Period"]) == ("ZeroAdsSessions", "FastChannels", 3600)
    assert "logs:StartQuery" in policies
    assert "log-group:/aws/vendedlogs/mediatailor/fast" in policies


def test_the_alerts_poller_runs_every_minute_on_the_census(ops: Template) -> None:
    function = _function(ops, "fast_channels.functions.ops.list_alerts.handler")
    schedule = _schedule(ops, "alerts of fast's running channels")
    policies = json.dumps(resources(ops, "AWS::IAM::Policy"))

    assert function["Environment"]["Variables"]["CENSUS_PARAMETER"] == "/fast/ops/running-channels"
    assert function["Environment"]["Variables"]["FAST_PREFIX"] == "fast"
    assert "EVENT_BUS" in function["Environment"]["Variables"]
    assert schedule["ScheduleExpression"] == "rate(1 minute)"
    assert json.loads(schedule["Target"]["Input"]) == {"scheduled_time": "<aws.scheduler.scheduled-time>"}
    assert "mediatailor:ListAlerts" in policies
    assert ":channel/fast-*" in policies
    assert "parameter/fast/ops/running-channels" in policies
    assert "events:PutEvents" in policies
