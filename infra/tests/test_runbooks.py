"""The ops module's runbooks, the ``flags`` profile and the probe canary, in the Ops stack's template."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.runbooks import Runbook
from fast_channels.infra.constructs.ops.canary import CANARY_DIR

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


def rules(template: Template) -> list[dict[str, Any]]:
    return [r["Properties"] for r in resources(template, "AWS::Events::Rule").values()]


def test_one_state_machine_per_runbook(ops: Template) -> None:
    machines = resources(ops, "AWS::StepFunctions::StateMachine").values()
    names = {m["Properties"]["StateMachineName"] for m in machines}

    assert names >= {f"fast-runbook-{runbook.value}" for runbook in Runbook}
    runbook_machine = next(m for m in machines if m["Properties"]["StateMachineName"] == "fast-runbook-r13")
    definition = json.dumps(runbook_machine["Properties"]["DefinitionString"])
    assert '\\"runbook\\":\\"r13\\"' in definition
    assert "JSONata" in definition


def test_stage_events_start_r13_r14_and_r15(ops: Template) -> None:
    patterns = {
        p["EventPattern"]["detail-type"][0]: p
        for p in rules(ops)
        if p["EventPattern"].get("source") == ["fast-channels.fast"]
    }

    assert {"Channel Alert", "Asset Failed", "Schedule Published", "Census Changed"} <= patterns.keys()
    assert all(p["Targets"][0]["RetryPolicy"] == {"MaximumRetryAttempts": 4} for p in patterns.values())


def test_alarms_start_r7_r9_r12_and_r17(ops: Template) -> None:
    by_alarms = {
        tuple(p["EventPattern"]["detail"]["alarmName"]): p
        for p in rules(ops)
        if p["EventPattern"].get("source") == ["aws.cloudwatch"]
    }

    assert set(by_alarms) == {
        ("fast-account-ad-insertion-requests", "fast-account-channel-assembly-requests"),
        ("fast-demo-getmanifest-errors",),
        ("fast-probe-off-air",),
        ("fast-cdn-5xx",),
    }
    assert all(p["EventPattern"]["detail"]["state"] == {"value": ["ALARM"]} for p in by_alarms.values())
    assert all("EventBusName" not in p for p in by_alarms.values())  # alarm state changes are on the default bus


def test_r17_needs_us_east_1(deployment: GetDeployment) -> None:
    d = deployment({"modules.ops.enabled": True}, region="eu-west-1")
    names = [
        p["EventPattern"]["detail"]["alarmName"]
        for p in rules(d.template(d.stage.ops))
        if p["EventPattern"].get("source") == ["aws.cloudwatch"]
    ]

    assert ["fast-cdn-5xx"] not in names


def test_r16_runs_every_hour(ops: Template) -> None:
    [schedule] = [
        s["Properties"]
        for s in resources(ops, "AWS::Scheduler::Schedule").values()
        if "R16" in s["Properties"].get("Description", "")
    ]

    assert schedule["ScheduleExpression"] == "rate(1 hour)"
    assert json.loads(schedule["Target"]["Input"]) == {"scheduled_time": "<aws.scheduler.scheduled-time>"}


def test_the_function_appends_only_to_runbook_chains(ops: Template) -> None:
    policies = [p["Properties"]["PolicyDocument"]["Statement"] for p in resources(ops, "AWS::IAM::Policy").values()]
    ledger = [
        s for statements in policies for s in statements if s.get("Action") == ["dynamodb:PutItem", "dynamodb:Query"]
    ]

    assert ledger
    assert all(s["Condition"] == {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["runbook/*"]}} for s in ledger)
    flags = [
        s for statements in policies for s in statements if "appconfig:GetLatestConfiguration" in s.get("Action", [])
    ]
    assert flags


def test_the_flags_profile_is_feature_flags_deployed_all_at_once(ops: Template) -> None:
    [profile] = resources(ops, "AWS::AppConfig::ConfigurationProfile").values()
    [version] = resources(ops, "AWS::AppConfig::HostedConfigurationVersion").values()
    [deployment] = resources(ops, "AWS::AppConfig::Deployment").values()

    assert (profile["Properties"]["Name"], profile["Properties"]["Type"]) == ("flags", "AWS.AppConfig.FeatureFlags")
    content = json.loads(version["Properties"]["Content"])
    assert content["values"]["ops"] == {
        "enabled": True,
        "act": False,
        "freeze": True,
        "restart": True,
        "ca_alerts": True,
        "shed": True,
    }
    assert content["values"]["demand"] == {"enabled": False, "mode": "off"}
    assert deployment["Properties"]["DeploymentStrategyId"] == "AppConfig.AllAtOnce"


def test_the_demand_mode_follows_the_configuration(deployment: GetDeployment) -> None:
    d = deployment({"modules.ops.enabled": True, "modules.demand.enabled": True})
    [version] = resources(d.template(d.stage.ops), "AWS::AppConfig::HostedConfigurationVersion").values()

    assert json.loads(version["Properties"]["Content"])["values"]["demand"] == {"enabled": True, "mode": "demo"}


def test_the_probe_canary_waits_for_the_census(ops: Template) -> None:
    [canary] = resources(ops, "AWS::Synthetics::Canary").values()
    props = canary["Properties"]

    assert props["Name"] == "fast-probe"
    assert props["RuntimeVersion"].startswith("syn-python-selenium-")
    assert props["Schedule"]["Expression"] == "rate(5 minutes)"
    assert props["StartCanaryAfterCreation"] is False
    assert props["ProvisionedResourceCleanup"] == "AUTOMATIC"
    assert props["Code"]["Handler"] == "fast_probe.handler"
    assert set(props["RunConfig"]["EnvironmentVariables"]) == {"CENSUS_PARAMETER", "CHANNELS"}
    assert (CANARY_DIR / "python" / "fast_probe.py").is_file()


def test_the_census_starts_and_stops_the_canary(ops: Template) -> None:
    [switch] = [
        m["Properties"]
        for m in resources(ops, "AWS::StepFunctions::StateMachine").values()
        if m["Properties"]["StateMachineName"] == "fast-probe-switch"
    ]
    definition = json.dumps(switch["DefinitionString"])

    assert "synthetics:startCanary" in definition
    assert "synthetics:stopCanary" in definition
    assert "Synthetics.ConflictException" in definition


def test_the_off_air_alarm_needs_two_failed_runs(ops: Template) -> None:
    [alarm] = [
        a["Properties"]
        for a in resources(ops, "AWS::CloudWatch::Alarm").values()
        if a["Properties"]["AlarmName"] == "fast-probe-off-air"
    ]

    assert (alarm["MetricName"], alarm["Namespace"]) == ("SuccessPercent", "CloudWatchSynthetics")
    assert (alarm["EvaluationPeriods"], alarm["Threshold"], alarm["TreatMissingData"]) == (2, 100, "notBreaching")
    assert alarm["ComparisonOperator"] == "LessThanThreshold"
