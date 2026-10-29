"""``observability.as_run``: a per-stage as-run subscription (v3.0), or an EventBridge rule on the account's router,
which ``make as-run-router`` deploys (``-c as-run-router=true``)."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from aws_cdk.assertions import Template

type GetDeployment = Callable[..., Any]


def resources(template: Template, kind: str) -> dict[str, Any]:
    found: dict[str, Any] = template.find_resources(kind)
    return found


def test_per_stage_is_v3(default: Any) -> None:
    media = default.template(default.stage.media)
    [subscription] = resources(media, "AWS::Logs::SubscriptionFilter").values()

    assert default.stage.as_run is None
    assert subscription["Properties"]["FilterName"] == "fast-as-run"
    assert not [r for r in resources(media, "AWS::Events::Rule").values() if "as-run" in json.dumps(r)]


def test_a_shared_stage_reads_the_default_bus_and_subscribes_nothing(deployment: GetDeployment) -> None:
    d = deployment({"observability.as_run": "shared"})
    media = d.template(d.stage.media)
    [rule] = [r for r in resources(media, "AWS::Events::Rule").values() if "fast-channels.as-run" in json.dumps(r)]

    assert resources(media, "AWS::Logs::SubscriptionFilter") == {}
    assert json.loads(json.dumps(rule["Properties"]["EventPattern"])) == {
        "source": ["fast-channels.as-run"],
        "detail-type": ["VodProgramStart"],
        "detail": {"channelName": [{"prefix": "fast-"}]},
    }
    assert "EventBusName" not in rule["Properties"]  # the default bus
    [target] = rule["Properties"]["Targets"]
    assert target["RetryPolicy"] == {"MaximumRetryAttempts": 8}
    assert d.stage.as_run is None  # no stage deploys or destroys the router


@pytest.mark.parametrize("flag", [True, "true", "TRUE"])
def test_only_the_context_flag_adds_the_router(deployment: GetDeployment, flag: object) -> None:
    assert deployment({"as-run-router": flag}).stage.as_run is not None
    assert deployment({"as-run-router": "false"}).stage.as_run is None


def test_with_ops_the_rule_also_passes_break_starts(deployment: GetDeployment) -> None:
    d = deployment({"observability.as_run": "shared", "modules.ops.enabled": True})
    [rule] = [
        r
        for r in resources(d.template(d.stage.media), "AWS::Events::Rule").values()
        if "fast-channels.as-run" in json.dumps(r)
    ]

    assert rule["Properties"]["EventPattern"]["detail-type"] == ["VodProgramStart", "AdBreakStart"]


def test_the_router_stack(deployment: GetDeployment) -> None:
    d = deployment({"as-run-router": True, "notifications.email": "ops@example.com"})
    stack = d.stage.as_run
    template = d.template(stack)
    [subscription] = resources(template, "AWS::Logs::SubscriptionFilter").values()
    [config] = resources(template, "AWS::Lambda::EventInvokeConfig").values()
    alarms = {r["Properties"]["AlarmName"] for r in resources(template, "AWS::CloudWatch::Alarm").values()}
    policies = json.dumps(resources(template, "AWS::IAM::Policy"))

    assert (stack.stack_name, stack.termination_protection) == ("FastChannels-AsRun", True)
    assert {dep.node.id for dep in stack.dependencies} == {"Media"}
    assert subscription["Properties"] == {
        "DestinationArn": subscription["Properties"]["DestinationArn"],
        "FilterName": "fast-channels-as-run-router",
        "FilterPattern": "",
        "LogGroupName": "MediaTailor/Channel/AsRunLog",
    }
    assert config["Properties"]["MaximumRetryAttempts"] == 2
    assert "OnFailure" in config["Properties"]["DestinationConfig"]
    assert alarms == {
        "fast-channels-as-run-router-errors",
        "fast-channels-as-run-router-throttles",
        "fast-channels-as-run-router-dead-letters",
    }
    assert "events:PutEvents" in policies
    assert ":event-bus/default" in policies
    template.has_resource_properties("AWS::SNS::Subscription", {"Endpoint": "ops@example.com"})
    [log_group] = resources(template, "AWS::Logs::LogGroup").values()
    assert log_group["DeletionPolicy"] == "Retain"
