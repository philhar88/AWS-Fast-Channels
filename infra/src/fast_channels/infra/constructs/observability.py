"""Media-plane observability (SPEC §5.5, §9, §10): alarms, the dashboard, MediaConvert job logs, the
running-channel census and the Logs Insights trace queries.

The alert topic and alarm recipes live in :mod:`~fast_channels.infra.constructs.alerts`, the event bus in
:mod:`~fast_channels.infra.constructs.events` and the log-group helpers in
:mod:`~fast_channels.infra.constructs.logs`; this module re-exports their names for v3.0 imports. Account-level
observability lives in ``observability_baseline``.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from fast_channels.core.events import (
    DETAIL_TYPES,
    AssetFailed,
    AutopilotRun,
    CensusChanged,
    LadderFailed,
    event_source,
)
from fast_channels.core.tags import TAG_APP
from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import (
    CLOUDFRONT_REGION,
    PERIOD,
    alarm,
    alert_topic,
    alert_topic_name,
    cdn_error_alarm,
    cdn_metric,
    dead_letter_alarm,
)
from fast_channels.infra.constructs.events import DEFAULT_ARCHIVE_RETENTION, FastEventBus
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE
from fast_channels.infra.constructs.logs import LogGroupTarget, log_group_arn, rule_log_group, vended_log_group
from fast_channels.infra.grants import allow

if TYPE_CHECKING:
    from fast_channels.infra.exports import ChannelExports

__all__ = [
    "CENSUS_INTERVAL",
    "CHANNEL_MANIFEST_TPS_QUOTA",
    "CLOUDFRONT_REGION",
    "DEFAULT_ARCHIVE_RETENTION",
    "QUOTA_ALARM_RATIO",
    "RUNNING_CHANNELS_METRIC",
    "TRACE_QUERIES",
    "CensusChanges",
    "ChannelCensus",
    "FastEventBus",
    "MediaObservability",
    "TraceQuery",
    "alert_topic",
    "cdn_error_alarm",
    "cdn_metric",
    "census_definition",
    "census_parameter_name",
    "log_group_arn",
    "rule_log_group",
    "vended_log_group",
]

CHANNEL_MANIFEST_TPS_QUOTA: Final = 50
"""Default Channel Assembly quota "Channel Manifest Requests" per channel; SSAI requests a channel about 1:1 per
viewer playlist refresh (e2e.md §6.2), so this caps a channel at a few hundred concurrent viewers."""
QUOTA_ALARM_RATIO: Final = 0.8
CENSUS_INTERVAL: Final = Duration.minutes(5)
RUNNING_CHANNELS_METRIC: Final = "RunningChannels"
_RULE_DLQ: Final = "Serverless-EventBusDLQ"


@dataclass(frozen=True, slots=True)
class TraceQuery:
    """A saved Logs Insights query that follows one identifier through every log of the deployment."""

    title: str
    parameter: str
    description: str
    fields: str

    def query_string(self) -> str:
        """Logs Insights QL with a ``{{parameter}}`` placeholder (prefix ``SOURCE logGroupTags(...)`` to run it
        over every log group tagged ``fast:app=<prefix>`` through the API)."""
        return (
            f"fields @timestamp, @log, {self.fields}, @message\n"
            f'| filter @message like "{{{{{self.parameter}}}}}"\n'
            "| sort @timestamp asc\n"
            "| limit 10000"
        )


TRACE_QUERIES: Final[tuple[TraceQuery, ...]] = (
    TraceQuery(
        "Trace asset",
        "asset_id",
        "Asset id or ingest correlation id (the ingest execution name)",
        "coalesce(correlation_id, detail.userMetadata.correlation_id, execution_arn) as correlation, "
        "coalesce(message, detail.status, details.detail_type, message_type, type) as step",
    ),
    TraceQuery(
        "Trace session",
        "session_id",
        "MediaTailor playback session id ([session.uuid], CMCD sid, aws.sessionId)",
        "coalesce(eventType, message_type) as step, coalesce(adsRequestUrl, message) as detail",
    ),
    TraceQuery(
        "Trace program",
        "program",
        "Program name or SCTE-35 event id of one of its breaks",
        "coalesce(eventType, message, details.detail_type, message_type) as step, "
        "coalesce(programName, correlation_id) as program",
    ),
    TraceQuery(
        "Trace agent session",
        "session_id",
        "AgentCore runtime session id, the correlation id of every tool call of a conversation",
        "coalesce(correlation_id, execution_arn) as correlation, "
        "coalesce(message, details.detail_type, message_type, type, eventType) as step",
    ),
)
"""Trace queries of SPEC §9; ``scripts/trace.py`` runs them. CloudFront logs are Parquet objects in the logs bucket
(``CdnAccessLogs``), outside Logs Insights: query them with Athena by ``cmcd-session-id``."""


@dataclass(frozen=True, slots=True)
class CensusChanges:
    """Where the census records its last count and announces that the count crossed zero (the ops module)."""

    parameter: str
    """Name of the SSM parameter holding the last count."""
    event_bus: str
    """Name of the deployment's event bus (``Census Changed`` events)."""


def census_parameter_name(prefix: str) -> str:
    """SSM parameter in which the census records the last count (with the ops module)."""
    return f"/{prefix}/ops/running-channels"


def census_definition(prefix: str, changes: CensusChanges | None = None) -> dict[str, Any]:
    """Express workflow counting this deployment's running Channel Assembly channels and publishing the count as
    ``FastChannels/RunningChannels{App=<prefix>}``.

    Channel Assembly publishes no metric while a channel runs without viewers, yet bills every running hour. A
    channel belongs to the deployment by its ``fast:app`` tag (IaC channels get it from the stage, agent channels
    from the tools; CONTRACTS §E): name prefixes would also match deployments whose prefix extends this one.

    With ``changes`` the workflow then compares the count with the last one it recorded: when one of them is 0 and
    the other is not, it puts a ``Census Changed`` event (``core.events.CensusChanged``) on the bus before it
    records the new count, so a failed put is retried by the next census (consumers see each change at least
    once). Without it the definition is v3.0's.
    """
    own = f"Tags.`{TAG_APP}` = '{prefix}'"
    definition: dict[str, Any] = {
        "Comment": f"Count the running channels tagged {TAG_APP}={prefix}",
        "QueryLanguage": "JSONata",
        "StartAt": "Start",
        "States": {
            "Start": {"Type": "Pass", "Assign": {"running": 0, "page": {"MaxResults": 100}}, "Next": "ListChannels"},
            "ListChannels": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:mediatailor:listChannels",
                "Arguments": "{% $page %}",
                "Assign": {
                    "running": f"{{% $running + $count($states.result.Items[ChannelState = 'RUNNING' and {own}]) %}}",
                    # JSONata drops keys whose value is undefined: the last page leaves no NextToken.
                    "page": "{% {'MaxResults': 100, 'NextToken': $states.result.NextToken} %}",
                },
                "Retry": [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 2}],
                "Next": "MorePages",
            },
            "MorePages": {
                "Type": "Choice",
                "Choices": [{"Condition": "{% $exists($page.NextToken) %}", "Next": "ListChannels"}],
                "Default": "PublishCount",
            },
            "PublishCount": {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:cloudwatch:putMetricData",
                "Arguments": {
                    "Namespace": METRICS_NAMESPACE,
                    "MetricData": [
                        {
                            "MetricName": RUNNING_CHANNELS_METRIC,
                            "Dimensions": [{"Name": "App", "Value": prefix}],
                            "Value": "{% $running %}",
                            "Unit": "Count",
                        }
                    ],
                },
                "End": True,
            },
        },
    }
    if changes is not None:
        publish = definition["States"]["PublishCount"]
        del publish["End"]
        publish["Next"] = "ReadLastCount"
        definition["States"] |= _census_change_states(prefix, changes)
    return definition


def _census_change_states(prefix: str, changes: CensusChanges) -> dict[str, Any]:
    """States after ``PublishCount`` that announce a zero crossing (:func:`census_definition`)."""
    retry = [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 2, "MaxAttempts": 2}]
    return {
        "ReadLastCount": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:ssm:getParameter",
            "Arguments": {"Name": changes.parameter},
            "Assign": {"previous": "{% $number($states.result.Parameter.Value) %}"},
            "Retry": retry,
            "Next": "Changed",
        },
        "Changed": {
            "Type": "Choice",
            "Choices": [
                {"Condition": "{% ($running = 0) != ($previous = 0) %}", "Next": "PublishCensusChanged"},
                {"Condition": "{% $running != $previous %}", "Next": "RecordCount"},
            ],
            "Default": "Unchanged",
        },
        "PublishCensusChanged": {
            "Type": "Task",
            "Resource": "arn:aws:states:::events:putEvents",
            "Arguments": {
                "Entries": [
                    {
                        "EventBusName": changes.event_bus,
                        "Source": event_source(prefix),
                        "DetailType": DETAIL_TYPES[CensusChanged],
                        "Detail": {
                            "running": "{% $running %}",
                            "previous": "{% $previous %}",
                            "correlation_id": "{% $states.context.Execution.Name %}",
                        },
                    }
                ]
            },
            "Retry": retry,
            "Next": "RecordCount",
        },
        "RecordCount": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:ssm:putParameter",
            "Arguments": {"Name": changes.parameter, "Value": "{% $string($running) %}", "Overwrite": True},
            "Retry": retry,
            "End": True,
        },
        "Unchanged": {"Type": "Succeed"},
    }


class ChannelCensus(Construct):
    """Runs :func:`census_definition` every :data:`CENSUS_INTERVAL` (EventBridge Scheduler, no retries: the next
    run follows shortly).

    Args:
        prefix: deployment prefix (the ``fast:app`` tag value counted).
        log_retention: retention of the workflow's log group.
        removal_policy: what happens to that log group when the stack is deleted.
        event_bus: put ``Census Changed`` on this bus when the count crosses zero (the ops module), recording the
            last count in the SSM parameter :func:`census_parameter_name`; ``None`` (v3.0) publishes the metric
            only.

    Attributes:
        state_machine: the census workflow.
        running_channels: its metric (maximum per period).
        channel_hours: running channel-hours per day (the census samples summed over a day).
        last_count: the SSM parameter with the last count, when ``event_bus`` is given. CloudFormation creates it
            at ``0`` and the census overwrites it; consumers read it as the current count (at most one census
            interval old).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        event_bus: FastEventBus | None = None,
    ) -> None:
        super().__init__(scope, id)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-channel-census",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self.last_count: ssm.StringParameter | None = None
        changes = None
        if event_bus is not None:
            changes = CensusChanges(parameter=census_parameter_name(prefix), event_bus=event_bus.name)
            self.last_count = ssm.StringParameter(
                self,
                "LastCount",
                parameter_name=changes.parameter,
                string_value="0",
                description=f"Running channels tagged {TAG_APP}={prefix} at the last census that saw a change",
            )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_string(json.dumps(census_definition(prefix, changes))),
            comment=f"Publishes the number of running channels tagged {TAG_APP}={prefix}",
            timeout=Duration.minutes(1),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
        )
        allow(self.state_machine, ["mediatailor:ListChannels"], ["*"])
        allow(
            self.state_machine,
            ["cloudwatch:PutMetricData"],
            ["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": METRICS_NAMESPACE}},
        )
        nag.acknowledge(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "mediatailor:ListChannels and cloudwatch:PutMetricData have no resource-level "
                "permissions (the latter is limited to the FastChannels namespace); X-Ray tracing needs '*'"
            },
        )
        if event_bus is not None and self.last_count is not None:
            allow(self.state_machine, ["ssm:GetParameter", "ssm:PutParameter"], [self.last_count.parameter_arn])
            allow(self.state_machine, ["events:PutEvents"], [event_bus.bus.event_bus_arn])
        scheduler.Schedule(
            self,
            "Schedule",
            description=f"Count the running channels tagged {TAG_APP}={prefix}",
            schedule=scheduler.ScheduleExpression.rate(CENSUS_INTERVAL),
            target=scheduler_targets.StepFunctionsStartExecution(self.state_machine, retry_attempts=0),
        )
        self.running_channels = cw.Metric(
            namespace=METRICS_NAMESPACE,
            metric_name=RUNNING_CHANNELS_METRIC,
            dimensions_map={"App": prefix},
            statistic=cw.Stats.MAXIMUM,
            period=PERIOD,
            label="Running channels",
        )
        samples_per_hour = Duration.hours(1).to_minutes() // CENSUS_INTERVAL.to_minutes()
        self.channel_hours = cw.MathExpression(
            expression=f"census / {samples_per_hour}",
            using_metrics={"census": self.running_channels.with_(statistic=cw.Stats.SUM, period=Duration.days(1))},
            period=Duration.days(1),
            label="Channel-hours per day",
        )


class MediaObservability(Construct):
    """Alarms, dashboard, MediaConvert job logs and trace queries of the media plane, notifying one SNS topic.

    - MediaConvert has no logs of its own: a default-bus rule sends the "MediaConvert Job State Change" events of
      this deployment's jobs (``userMetadata.app``) to ``/aws/vendedlogs/events/<prefix>-mediaconvert``
      (mediaconvert.md §7.5); they include warnings such as 230001.
    - Alarms: failed executions of each state machine, messages in each dead-letter queue, MediaConvert job errors
      on the queue, per channel the Channel Assembly errors and manifest requests at :data:`QUOTA_ALARM_RATIO` of
      ``channel_manifest_tps_quota``, per playback configuration ADS errors, timeouts and fill rate, running
      channel-hours per day (:class:`ChannelCensus`) and, when this stack is in us-east-1, the CloudFront 5xx rate.
    - Asset Failed, Ladder Failed and failed Autopilot Run events on ``event_bus`` are forwarded to the topic, by
      rules that publish through their own role: the topic admits no service principal, so no other account's or
      application's rule can post to the operators' alert email.
    - Saved queries :data:`TRACE_QUERIES` over the bus and MediaConvert logs plus ``trace_log_groups``.

    Args:
        prefix: deployment prefix (``userMetadata.app`` of its MediaConvert jobs, ``fast:app`` tag value).
        event_bus: the deployment's :class:`FastEventBus`.
        state_machines: state machines to alarm on, by label (e.g. ``{"ingest": ..., "ladder provisioning": ...}``).
        mediaconvert_queue_arn: the deployment's MediaConvert queue.
        channels: IaC channels (``MediaStack`` channel exports); agent channels appear in the census only.
        distribution: the media distribution.
        log_retention: retention of the log groups created here.
        removal_policy: what happens to them when the stack is deleted.
        notification_email: subscribed to the topic.
        trace_log_groups: further log groups for the trace queries (pipeline functions and state machines,
            MediaTailor vended logs, as-run logs).
        dead_letter_queues: dead-letter queues to alarm on, by label (e.g. ``{"ingest": ...}``).
        channel_manifest_tps_quota: the account's "Channel Manifest Requests" quota.
        max_channel_hours_per_day: running channel-hours per day that raise the cost alarm.
        census_changes: the census also announces zero crossings on ``event_bus`` (``Census Changed``; the ops
            module).

    Attributes:
        topic: the alert topic.
        mediaconvert_log_group: MediaConvert job events.
        census: the running-channel census.
        alarms: every alarm, by name.
        dashboard: the CloudWatch dashboard.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        event_bus: FastEventBus,
        state_machines: Mapping[str, sfn.IStateMachine],
        mediaconvert_queue_arn: str,
        channels: Sequence[ChannelExports],
        distribution: cf.IDistribution,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        notification_email: str | None = None,
        trace_log_groups: Sequence[logs.ILogGroup] = (),
        dead_letter_queues: Mapping[str, sqs.IQueue] | None = None,
        channel_manifest_tps_quota: int = CHANNEL_MANIFEST_TPS_QUOTA,
        max_channel_hours_per_day: float = 24,
        census_changes: bool = False,
    ) -> None:
        super().__init__(scope, id)
        self._prefix = prefix
        stack = Stack.of(self)
        self.topic = alert_topic(self, "Topic", name=alert_topic_name(prefix), email=notification_email)
        self.mediaconvert_log_group = self._mediaconvert_job_logs(log_retention, removal_policy)
        self._forward_failures(event_bus.bus)
        self.census = ChannelCensus(
            self,
            "Census",
            prefix=prefix,
            log_retention=log_retention,
            removal_policy=removal_policy,
            event_bus=event_bus if census_changes else None,
        )
        self.alarms: dict[str, cw.Alarm] = {}
        for label, state_machine in state_machines.items():
            self._alarm(
                f"{prefix} {label} failed",
                state_machine.metric_failed(period=PERIOD, statistic=cw.Stats.SUM),
                threshold=1,
                description=f"A {label} execution failed",
            )
        for label, queue in (dead_letter_queues or {}).items():
            name = f"{prefix}-{label}-dead-letters".replace(" ", "-")
            self.alarms[name] = dead_letter_alarm(
                self,
                _construct_id(name.removeprefix(f"{prefix}-")),
                queue=queue,
                name=name,
                description=f"Messages in the {label} dead-letter queue: requests that failed before anything ran; "
                "inspect and redrive or purge them",
                topic=self.topic,
            )
        jobs = _mediaconvert_metrics(mediaconvert_queue_arn)
        self._alarm(
            f"{prefix} mediaconvert job errors", jobs["JobsErroredCount"], threshold=1, description="A job failed"
        )
        for channel in channels:
            self._channel_alarms(channel, channel_manifest_tps_quota)
        self._alarm(
            f"{prefix} channel hours",
            self.census.channel_hours,
            threshold=max_channel_hours_per_day,
            description=f"Channels ran more than {max_channel_hours_per_day:g} channel-hours in a day (each "
            "running hour is billed)",
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        )
        if stack.region == CLOUDFRONT_REGION:
            self.alarms[f"{prefix}-cdn-5xx"] = cdn_error_alarm(
                self, "CdnErrors", distribution=distribution, topic=self.topic, name=f"{prefix}-cdn-5xx"
            )
        trace_groups = [event_bus.log_group, self.mediaconvert_log_group, *trace_log_groups]
        for query in TRACE_QUERIES:
            logs.CfnQueryDefinition(
                self,
                query.title.title().replace(" ", ""),
                name=f"FastChannels-{prefix}/{query.title}",
                query_string=query.query_string(),
                log_group_names=[group.log_group_name for group in trace_groups],
                parameters=[
                    logs.CfnQueryDefinition.QueryParameterProperty(name=query.parameter, description=query.description)
                ],
            )
        self.dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"FastChannels-{prefix}-{stack.region}",
            default_interval=Duration.hours(6),
            widgets=_dashboard_rows(
                prefix=prefix,
                alarms=list(self.alarms.values()),
                state_machines=state_machines,
                jobs=jobs,
                census=self.census,
                channels=channels,
                distribution=distribution,
                mediaconvert_logs=self.mediaconvert_log_group,
                tps_quota=channel_manifest_tps_quota,
            ),
        )

    def _alarm(
        self,
        title: str,
        metric: cw.IMetric,
        *,
        threshold: float,
        description: str,
        comparison: cw.ComparisonOperator = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluation_periods: int = 1,
    ) -> None:
        """Alarm named ``title`` with hyphens for spaces, notifying the topic of both state changes. Its construct id
        leaves out the deployment prefix, so that renames never meet a same-named alarm."""
        name = title.replace(" ", "-")
        self.alarms[name] = alarm(
            self,
            _construct_id(name.removeprefix(f"{self._prefix}-")),
            name=name,
            metric=metric,
            threshold=threshold,
            description=description,
            topic=self.topic,
            comparison=comparison,
            evaluation_periods=evaluation_periods,
        )

    def _channel_alarms(self, channel: ChannelExports, tps_quota: int) -> None:
        requests, errors = _channel_metrics(channel.name)
        per_minute = QUOTA_ALARM_RATIO * tps_quota * 60
        self._alarm(
            f"{channel.name} manifest requests",
            requests.with_(period=Duration.minutes(1)),
            threshold=per_minute,
            evaluation_periods=3,
            description=f"{channel.name}: manifest requests above {QUOTA_ALARM_RATIO:.0%} of the {tps_quota} TPS "
            "quota; request an increase in Service Quotas (MediaTailor, Channel Manifest Requests)",
        )
        self._alarm(
            f"{channel.name} errors",
            errors,
            threshold=10,
            description=f"{channel.name}: Channel Assembly answered 4xx/5xx (channel policy, schedule or source)",
        )
        ads = _ads_metrics(channel.playback_configuration_name)
        self._alarm(
            f"{channel.name} ads errors",
            ads["errors"],
            threshold=5,
            description=f"{channel.playback_configuration_name}: ad decision server errors or timeouts",
        )
        self._alarm(
            f"{channel.name} fill rate",
            ads["fill_rate"],
            threshold=0.5,
            evaluation_periods=3,
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            description=f"{channel.playback_configuration_name}: under half of the ad avails filled for 15 minutes",
        )

    def _mediaconvert_job_logs(self, retention: logs.RetentionDays, removal_policy: RemovalPolicy) -> logs.LogGroup:
        rule_name = f"{self._prefix}-mediaconvert-jobs"
        log_group = rule_log_group(
            self,
            "MediaConvertJobs",
            name=f"/aws/vendedlogs/events/{self._prefix}-mediaconvert",
            rule_name=rule_name,
            retention=retention,
            removal_policy=removal_policy,
        )
        rule = events.Rule(
            self,
            "MediaConvertJobsRule",
            rule_name=rule_name,
            description=f"MediaConvert job state changes of {self._prefix} jobs to CloudWatch Logs",
            event_pattern=events.EventPattern(
                source=["aws.mediaconvert"],
                detail_type=["MediaConvert Job State Change"],
                detail={"userMetadata": {"app": [self._prefix]}},
            ),
            targets=[LogGroupTarget(log_group)],
        )
        nag.acknowledge(
            rule, {_RULE_DLQ: "Log copy of job states: EventBridge retries for 24 h and MediaConvert keeps each job"}
        )
        return log_group

    def _forward_failures(self, bus: events.IEventBus) -> None:
        source = [event_source(self._prefix)]
        failures = [
            events.EventPattern(source=source, detail_type=[DETAIL_TYPES[AssetFailed], DETAIL_TYPES[LadderFailed]]),
            events.EventPattern(source=source, detail_type=[DETAIL_TYPES[AutopilotRun]], detail={"status": ["FAILED"]}),
        ]
        message = events.RuleTargetInput.from_object(
            {
                "alert": f"FAST Channels ({self._prefix}): {events.EventField.detail_type}",
                "detail": events.EventField.from_path("$.detail"),
            }
        )
        for index, pattern in enumerate(failures):
            rule = events.Rule(
                self,
                f"NotifyFailures{index}",
                event_bus=bus,
                description="Notify the alert topic of failed assets, ladders and autopilot runs",
                event_pattern=pattern,
                # A rule role instead of an events.amazonaws.com topic-policy statement (confused deputy).
                targets=[events_targets.SnsTopic(self.topic, message=message, authorize_using_role=True)],
            )
            nag.acknowledge(rule, {_RULE_DLQ: "Notification: EventBridge retries for 24 h; the bus archive keeps it"})


def _construct_id(name: str) -> str:
    """``ingest-failed`` -> ``IngestFailedAlarm``."""
    return "".join(part.capitalize() for part in name.split("-")) + "Alarm"


def _mediaconvert_metrics(queue_arn: str) -> dict[str, cw.Metric]:
    return {
        name: cw.Metric(
            namespace="AWS/MediaConvert",
            metric_name=name,
            dimensions_map={"Queue": queue_arn},
            statistic=cw.Stats.SUM,
            period=PERIOD,
        )
        for name in ("JobsSubmittedCount", "JobsCompletedCount", "JobsErroredCount")
    }


def _channel_metrics(channel: str) -> tuple[cw.Metric, cw.MathExpression]:
    def metric(name: str) -> cw.Metric:
        return cw.Metric(
            namespace="AWS/MediaTailor",
            metric_name=name,
            dimensions_map={"Channel": channel},
            statistic=cw.Stats.SUM,
            period=PERIOD,
        )

    e4, e5 = _metric_id("e4", channel), _metric_id("e5", channel)
    errors = cw.MathExpression(
        expression=f"FILL({e4}, 0) + FILL({e5}, 0)",
        using_metrics={e4: metric("4xxErrorCount"), e5: metric("5xxErrorCount")},
        period=PERIOD,
        label=f"{channel} 4xx + 5xx",
    )
    return metric("RequestCount"), errors


def _metric_id(name: str, resource: str) -> str:
    """Math-expression metric id unique per resource: a dashboard graph of several channels shares one id space."""
    return f"{name}_{resource.replace('-', '_')}"


def _ads_metrics(configuration: str) -> dict[str, cw.IMetric]:
    def metric(name: str, statistic: str) -> cw.Metric:
        return cw.Metric(
            namespace="AWS/MediaTailor",
            metric_name=name,
            dimensions_map={"ConfigurationName": configuration},
            statistic=statistic,
            period=PERIOD,
        )

    errors, timeouts = _metric_id("errors", configuration), _metric_id("timeouts", configuration)
    return {
        "errors": cw.MathExpression(
            expression=f"FILL({errors}, 0) + FILL({timeouts}, 0)",
            using_metrics={
                errors: metric("AdDecisionServer.Errors", cw.Stats.SUM),
                timeouts: metric("AdDecisionServer.Timeouts", cw.Stats.SUM),
            },
            period=PERIOD,
            label=f"{configuration} ADS errors + timeouts",
        ),
        # A ratio 0-1, verified on the e2e playback configurations.
        "fill_rate": metric("Avail.FillRate", cw.Stats.AVERAGE),
        "avails": metric("Avail.Duration", cw.Stats.SAMPLE_COUNT),
        "requests": metric("Requests", cw.Stats.SUM),
    }


def _dashboard_rows(
    *,
    prefix: str,
    alarms: Sequence[cw.Alarm],
    state_machines: Mapping[str, sfn.IStateMachine],
    jobs: Mapping[str, cw.Metric],
    census: ChannelCensus,
    channels: Sequence[ChannelExports],
    distribution: cf.IDistribution,
    mediaconvert_logs: logs.ILogGroup,
    tps_quota: int,
) -> list[list[cw.IWidget]]:
    def graph(title: str, left: Sequence[cw.IMetric], **options: Any) -> cw.GraphWidget:
        return cw.GraphWidget(title=title, left=list(left), width=8, height=6, **options)

    # Requests per 5-minute point at the alarm's share of the per-second quota.
    quota = cw.HorizontalAnnotation(
        value=QUOTA_ALARM_RATIO * tps_quota * PERIOD.to_seconds(),
        label=f"{QUOTA_ALARM_RATIO * 100:g} % of the {tps_quota} TPS quota",
    )
    ads = [_ads_metrics(c.playback_configuration_name) for c in channels]
    return [
        [
            cw.TextWidget(
                markdown=f"# FAST Channels ({prefix})\nIngest, channels, ad insertion and delivery. Trace an asset, "
                "session, program or agent session with the saved Logs Insights queries "
                f"*FastChannels-{prefix}/…*.",
                width=24,
                height=2,
            )
        ],
        [cw.AlarmStatusWidget(title="Alarms", alarms=list(alarms), width=24, height=4)],
        [
            graph(
                "Workflow executions",
                [
                    metric
                    for label, sm in state_machines.items()
                    for metric in (
                        sm.metric_succeeded(period=PERIOD, label=f"{label} succeeded"),
                        sm.metric_failed(period=PERIOD, label=f"{label} failed"),
                    )
                ],
            ),
            graph("MediaConvert jobs", list(jobs.values())),
            cw.LogQueryWidget(
                title="MediaConvert errors and warnings",
                log_group_names=[mediaconvert_logs.log_group_name],
                query_string="fields @timestamp, detail.status, detail.userMetadata.asset_id, detail.jobId, "
                "detail.errorMessage, detail.warnings.0.code\n"
                '| filter detail.status in ["ERROR", "NEW_WARNING"]\n| sort @timestamp desc\n| limit 50',
                width=8,
                height=6,
            ),
        ],
        [
            graph("Running channels", [census.running_channels], right=[census.channel_hours]),
            graph(
                "Channel Assembly manifest requests (5 min)",
                [_channel_metrics(c.name)[0] for c in channels],
                left_annotations=[quota],
            ),
            graph("Channel Assembly errors", [_channel_metrics(c.name)[1] for c in channels]),
        ],
        [
            graph("Ad fill rate", [a["fill_rate"] for a in ads], left_y_axis=cw.YAxisProps(min=0, max=1)),
            graph("Ad decision server errors and timeouts", [a["errors"] for a in ads]),
            graph("Playback requests and avails", [a["requests"] for a in ads], right=[a["avails"] for a in ads]),
        ],
        [
            graph("CloudFront requests", [cdn_metric(distribution, "Requests", statistic=cw.Stats.SUM)]),
            graph(
                "CloudFront error rates (%)",
                [
                    cdn_metric(distribution, "4xxErrorRate", statistic=cw.Stats.AVERAGE),
                    cdn_metric(distribution, "5xxErrorRate", statistic=cw.Stats.AVERAGE),
                ],
            ),
            graph("CloudFront bytes downloaded", [cdn_metric(distribution, "BytesDownloaded", statistic=cw.Stats.SUM)]),
        ],
    ]
