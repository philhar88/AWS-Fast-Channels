"""FastEventBus, MediaObservability and ChannelCensus in isolated stacks."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aws_cdk import App, Environment, Stack, Validations
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks, ServerlessChecks

from fast_channels.infra.constructs.observability import (
    TRACE_QUERIES,
    FastEventBus,
    MediaObservability,
    TraceQuery,
    census_definition,
)
from fast_channels.infra.stacks.media import ChannelExports

ACCOUNT = "123456789012"
REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT: dict[str, Any] = json.loads((REPO_ROOT / "cdk.json").read_text())["context"]
QUEUE = f"arn:aws:mediaconvert:us-east-1:{ACCOUNT}:queues/fast"
CHANNEL = ChannelExports(
    name="fast-demo",
    arn=f"arn:aws:mediatailor:us-east-1:{ACCOUNT}:channel/fast-demo",
    ladder_version="standard@86bf8088e0",
    outputs=("hls", "dash-avc"),
    playback_configuration_name="fast-fast-demo",
    hls_prefix="https://abc.mediatailor.us-east-1.amazonaws.com/v1/master/h/fast-fast-demo/",
    dash_prefix="https://abc.mediatailor.us-east-1.amazonaws.com/v1/dash/h/fast-fast-demo/",
)


def new_stack(region: str = "us-east-1", *, outdir: Path | None = None) -> Stack:
    app = App(context=CONTEXT, outdir=str(outdir) if outdir else None)
    return Stack(app, "Test", env=Environment(account=ACCOUNT, region=region))


def observability(stack: Stack, **props: Any) -> MediaObservability:
    bus = FastEventBus(stack, "Events", prefix="fast", log_retention=logs.RetentionDays.ONE_WEEK)
    ingest = sfn.StateMachine.from_state_machine_name(stack, "Ingest", "fast-ingest")
    distribution = cf.Distribution.from_distribution_attributes(
        stack, "Cdn", distribution_id="E2CDN", domain_name="d123.cloudfront.net"
    )
    extra = logs.LogGroup.from_log_group_name(stack, "Extra", "/aws/vendedlogs/states/fast-ingest")
    return MediaObservability(
        stack,
        "Observability",
        prefix="fast",
        event_bus=bus,
        state_machines={"ingest": ingest},
        mediaconvert_queue_arn=QUEUE,
        channels=[CHANNEL],
        distribution=distribution,
        log_retention=logs.RetentionDays.ONE_WEEK,
        trace_log_groups=[extra],
        **props,
    )


@pytest.fixture(scope="module")
def template() -> Template:
    stack = new_stack()
    observability(stack, notification_email="ops@example.com")
    return Template.from_stack(stack)


def resources(template: Template, type_: str) -> Mapping[str, Mapping[str, Any]]:
    found: Mapping[str, Mapping[str, Any]] = template.find_resources(type_)
    return found


def log_group(template: Template, name: str) -> Mapping[str, Any]:
    [found] = [
        r for r in resources(template, "AWS::Logs::LogGroup").values() if r["Properties"]["LogGroupName"] == name
    ]
    return found


def alarms(template: Template) -> dict[str, Mapping[str, Any]]:
    return {
        a["Properties"]["AlarmName"]: a["Properties"] for a in resources(template, "AWS::CloudWatch::Alarm").values()
    }


def test_event_bus_with_archive_and_info_logs(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Events::EventBus",
        {"Name": "fast-fast-channels", "LogConfig": {"IncludeDetail": "FULL", "Level": "INFO"}},
    )
    template.has_resource_properties(
        "AWS::Events::Archive",
        {"ArchiveName": "fast-fast-channels", "EventPattern": {"account": [ACCOUNT]}, "RetentionDays": 30},
    )
    template.has_resource_properties(
        "AWS::Logs::DeliverySource",
        {
            "Name": "fast-fast-channels-info",
            "LogType": "INFO_LOGS",
            "ResourceArn": {"Fn::GetAtt": [Match.any_value(), "Arn"]},
        },
    )
    template.has_resource_properties(
        "AWS::Logs::DeliveryDestination",
        {
            "Name": "fast-fast-channels-logs",
            "OutputFormat": "json",
            "DestinationResourceArn": {
                "Fn::Join": ["", [f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:", {"Ref": Match.any_value()}]]
            },
        },
    )
    [statement] = log_group(template, "/aws/vendedlogs/events/fast-fast-channels")["Properties"][
        "ResourcePolicyDocument"
    ]["Statement"]
    assert statement["Principal"] == {"Service": "delivery.logs.amazonaws.com"}
    assert statement["Condition"]["ArnLike"] == {
        "aws:SourceArn": f"arn:aws:logs:us-east-1:{ACCOUNT}:delivery-source:fast-fast-channels-*"
    }


def test_mediaconvert_job_states_of_this_deployment_go_to_a_log_group(template: Template) -> None:
    name = "/aws/vendedlogs/events/fast-mediaconvert"
    [rule] = [r for r in resources(template, "AWS::Events::Rule").values() if r["Properties"].get("Name")]
    group = log_group(template, name)
    [statement] = group["Properties"]["ResourcePolicyDocument"]["Statement"]

    assert rule["Properties"]["Name"] == "fast-mediaconvert-jobs"
    assert "EventBusName" not in rule["Properties"]  # the default bus receives MediaConvert's events
    assert rule["Properties"]["EventPattern"] == {
        "source": ["aws.mediaconvert"],
        "detail-type": ["MediaConvert Job State Change"],
        "detail": {"userMetadata": {"app": ["fast"]}},
    }
    [target] = rule["Properties"]["Targets"]
    [group_id] = [
        k for k, r in resources(template, "AWS::Logs::LogGroup").items() if r["Properties"]["LogGroupName"] == name
    ]
    assert target["Arn"] == {"Fn::Join": ["", [f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:", {"Ref": group_id}]]}
    assert "RoleArn" not in target  # CloudWatch Logs targets authorise through the resource policy
    assert (group["Properties"]["RetentionInDays"], group["DeletionPolicy"]) == (7, "Delete")
    assert statement == {
        "Sid": "EventBridgeRuleTarget",
        "Effect": "Allow",
        "Principal": {"Service": "events.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:{name}:*",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": ACCOUNT},
            "ArnEquals": {"aws:SourceArn": f"arn:aws:events:us-east-1:{ACCOUNT}:rule/fast-mediaconvert-jobs"},
        },
    }


def test_no_account_level_resource_policies(template: Template) -> None:
    template.resource_count_is("AWS::Logs::ResourcePolicy", 0)
    assert not [r for r in template.to_json()["Resources"].values() if r["Type"].startswith("Custom::")]


def test_every_log_group_has_retention(template: Template) -> None:
    assert {r["Properties"].get("RetentionInDays") for r in resources(template, "AWS::Logs::LogGroup").values()} == {7}


def test_alarms_and_their_thresholds(template: Template) -> None:
    found = alarms(template)

    assert set(found) == {
        "fast-ingest-failed",
        "fast-mediaconvert-job-errors",
        "fast-demo-manifest-requests",
        "fast-demo-errors",
        "fast-demo-ads-errors",
        "fast-demo-fill-rate",
        "fast-channel-hours",
        "fast-cdn-5xx",
    }
    requests = found["fast-demo-manifest-requests"]
    assert (requests["MetricName"], requests["Namespace"], requests["Period"]) == (
        "RequestCount",
        "AWS/MediaTailor",
        60,
    )
    assert (requests["Threshold"], requests["EvaluationPeriods"]) == (2400, 3)  # 80 % of 50 TPS per minute
    assert requests["Dimensions"] == [{"Name": "Channel", "Value": "fast-demo"}]
    fill = found["fast-demo-fill-rate"]
    assert (fill["MetricName"], fill["Threshold"], fill["ComparisonOperator"]) == (
        "Avail.FillRate",
        0.5,
        "LessThanThreshold",
    )
    assert fill["Dimensions"] == [{"Name": "ConfigurationName", "Value": "fast-fast-demo"}]
    jobs = found["fast-mediaconvert-job-errors"]
    assert (jobs["MetricName"], jobs["Dimensions"]) == ("JobsErroredCount", [{"Name": "Queue", "Value": QUEUE}])
    assert found["fast-cdn-5xx"]["Dimensions"] == [
        {"Name": "DistributionId", "Value": "E2CDN"},
        {"Name": "Region", "Value": "Global"},
    ]
    assert {a["TreatMissingData"] for a in found.values()} == {"notBreaching"}


def test_channel_hours_alarm_sums_a_day_of_census_samples(template: Template) -> None:
    alarm = alarms(template)["fast-channel-hours"]
    [expression, census] = alarm["Metrics"]

    assert (expression["Expression"], alarm["Threshold"], alarm["ComparisonOperator"]) == (
        "census / 12",
        24,
        "GreaterThanThreshold",
    )
    assert census["MetricStat"] == {
        "Metric": {
            "MetricName": "RunningChannels",
            "Namespace": "FastChannels",
            "Dimensions": [{"Name": "App", "Value": "fast"}],
        },
        "Period": 86400,
        "Stat": "Sum",
    }


def test_every_alarm_notifies_the_topic(template: Template) -> None:
    [topic] = resources(template, "AWS::SNS::Topic")

    for alarm in alarms(template).values():
        assert alarm["AlarmActions"] == alarm["OKActions"] == [{"Ref": topic}]
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com", "TopicArn": {"Ref": topic}}
    )


def test_failed_assets_ladders_and_autopilot_runs_are_notified(template: Template) -> None:
    patterns = [
        r["Properties"]["EventPattern"]
        for r in resources(template, "AWS::Events::Rule").values()
        if "EventBusName" in r["Properties"]
    ]

    assert patterns == [
        {"source": ["fast-channels.fast"], "detail-type": ["Asset Failed", "Ladder Failed"]},
        {"source": ["fast-channels.fast"], "detail-type": ["Autopilot Run"], "detail": {"status": ["FAILED"]}},
    ]


def test_cdn_alarm_only_in_cloudfront_s_region() -> None:
    stack = new_stack("eu-west-1")
    observability(stack)
    template = Template.from_stack(stack)

    assert "fast-cdn-5xx" not in alarms(template)
    template.resource_count_is("AWS::SNS::Subscription", 0)


def test_census_counts_running_channels_every_five_minutes(template: Template) -> None:
    [machine] = resources(template, "AWS::StepFunctions::StateMachine").values()
    props = machine["Properties"]
    [policy] = [
        p for p in resources(template, "AWS::IAM::Policy").values() if "Census" in json.dumps(p["Properties"]["Roles"])
    ]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]

    assert props["StateMachineType"] == "EXPRESS"
    assert json.loads(props["DefinitionString"]) == census_definition("fast")
    assert props["TracingConfiguration"] == {"Enabled": True}
    assert props["LoggingConfiguration"]["Level"] == "ALL"
    assert props["LoggingConfiguration"]["IncludeExecutionData"] is True
    assert [s["Resource"] for s in statements if "mediatailor:ListChannels" in s["Action"]] == ["*"]
    assert {
        "Action": "cloudwatch:PutMetricData",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "FastChannels"}},
        "Effect": "Allow",
        "Resource": "*",
    } in statements
    template.has_resource_properties(
        "AWS::Scheduler::Schedule",
        {
            "ScheduleExpression": "rate(5 minutes)",
            "Target": Match.object_like({"RetryPolicy": {"MaximumRetryAttempts": 0}}),
        },
    )
    log_group(template, "/aws/vendedlogs/states/fast-channel-census")


def test_census_definition_counts_only_this_prefix() -> None:
    states = census_definition("acme")["States"]

    assert list(states) == ["Start", "ListChannels", "MorePages", "PublishCount"]
    assert "Tags.`fast:app` = 'acme'" in states["ListChannels"]["Assign"]["running"]
    assert states["ListChannels"]["Arguments"] == "{% $page %}"
    assert states["PublishCount"]["Arguments"]["MetricData"][0]["Dimensions"] == [{"Name": "App", "Value": "acme"}]


def test_saved_trace_queries(template: Template) -> None:
    queries = {
        q["Properties"]["Name"]: q["Properties"] for q in resources(template, "AWS::Logs::QueryDefinition").values()
    }

    assert set(queries) == {f"FastChannels-fast/{q.title}" for q in TRACE_QUERIES}
    asset = queries["FastChannels-fast/Trace asset"]
    assert asset["Parameters"] == [{"Name": "asset_id", "Description": TRACE_QUERIES[0].description}]
    assert 'filter @message like "{{asset_id}}"' in asset["QueryString"]
    assert len(asset["LogGroupNames"]) == 3
    assert "/aws/vendedlogs/states/fast-ingest" in asset["LogGroupNames"]


def test_trace_query_string() -> None:
    query = TraceQuery("Trace x", "x_id", "An id", "a as b")

    assert query.query_string() == (
        'fields @timestamp, @log, a as b, @message\n| filter @message like "{{x_id}}"\n'
        "| sort @timestamp asc\n| limit 10000"
    )


def test_dashboard(template: Template) -> None:
    [dashboard] = resources(template, "AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dashboard["Properties"]["DashboardBody"])

    assert dashboard["Properties"]["DashboardName"] == "FastChannels-fast-us-east-1"
    for needle in (
        "RunningChannels",
        "RequestCount",
        "Avail.FillRate",
        "5xxErrorRate",
        "JobsErroredCount",
        "NEW_WARNING",
    ):
        assert needle in body
    assert template.to_json()["Outputs"]["DashboardName"]["Value"] == {
        "Ref": next(iter(resources(template, "AWS::CloudWatch::Dashboard")))
    }


def test_no_unacknowledged_nag_findings(tmp_path: Path) -> None:
    stack = new_stack(outdir=tmp_path)
    observability(stack, notification_email="ops@example.com")
    app = stack.node.root
    assert isinstance(app, App)
    Validations.of(app).add_plugins(AwsSolutionsChecks(app), ServerlessChecks(app))

    app.synth()

    reports = json.loads((tmp_path / "validation-report.json").read_text())["pluginReports"]
    assert [v for r in reports for v in r.get("violations", [])] == []
