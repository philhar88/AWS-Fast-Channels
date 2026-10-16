"""Media-plane observability (SPEC §5.5, §9, §10): the ``fast-channels`` event bus, MediaConvert job logs, alarms,
the dashboard and the Logs Insights trace queries.

Log groups that AWS services write to carry their own resource policy (``AWS::Logs::LogGroup``
``ResourcePolicyDocument``): account-level ``AWS::Logs::ResourcePolicy`` documents are limited to 10 per region and
shared by every application in the account (e2e.md §2.4, _review.md). Account-level observability lives in
``observability_baseline``.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

import jsii
from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk.interfaces.aws_events import IRuleRef  # noqa: TC002 - jsii resolves bind() annotations at runtime
from constructs import Construct, IConstruct

from fast_channels.core.events import DETAIL_TYPES, AssetFailed, AutopilotRun, LadderFailed, event_source
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE

if TYPE_CHECKING:
    from fast_channels.infra.stacks.media import ChannelExports

CLOUDFRONT_REGION: Final = "us-east-1"
"""Region of CloudFront's metrics and log delivery sources."""
CHANNEL_MANIFEST_TPS_QUOTA: Final = 50
"""Default Channel Assembly quota "Channel Manifest Requests" per channel; SSAI requests a channel about 1:1 per
viewer playlist refresh (e2e.md §6.2), so this caps a channel at a few hundred concurrent viewers."""
QUOTA_ALARM_RATIO: Final = 0.8
CENSUS_INTERVAL: Final = Duration.minutes(5)
RUNNING_CHANNELS_METRIC: Final = "RunningChannels"
DEFAULT_ARCHIVE_RETENTION: Final = Duration.days(30)
_PERIOD: Final = Duration.minutes(5)
_SNS_SSE: Final = "AwsSolutions-SNS2"
_SNS_REDRIVE: Final = "Serverless-SNSRedrivePolicy"
_RULE_DLQ: Final = "Serverless-EventBusDLQ"


def log_group_arn(scope: IConstruct, name: str) -> str:
    """ARN of the log group ``name`` in ``scope``'s stack, without the ``:*`` suffix of ``LogGroup.log_group_arn``."""
    return Stack.of(scope).format_arn(
        service="logs", resource="log-group", resource_name=name, arn_format=ArnFormat.COLON_RESOURCE_NAME
    )


def _service_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    statement: Mapping[str, Any],
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy,
    log_group_class: logs.LogGroupClass,
) -> logs.LogGroup:
    log_group = logs.LogGroup(
        scope,
        id,
        log_group_name=name,
        retention=retention,
        removal_policy=removal_policy,
        log_group_class=log_group_class,
    )
    cfn = cast("logs.CfnLogGroup", log_group.node.default_child)
    cfn.resource_policy_document = {"Version": "2012-10-17", "Statement": [statement]}
    return log_group


def vended_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    delivery_sources: str,
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy,
    log_group_class: logs.LogGroupClass = logs.LogGroupClass.STANDARD,
) -> logs.LogGroup:
    """A log group that vended-log deliveries from this account's delivery sources named ``delivery_sources`` (a
    pattern such as ``"fast-cdn-*"``) may write to (e2e.md §2.4; deployed through CloudFormation for the CloudFront
    and event-bus deliveries, 2026-10-16)."""
    stack = Stack.of(scope)
    source_arn = stack.format_arn(
        service="logs",
        resource="delivery-source",
        resource_name=delivery_sources,
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )
    statement = {
        "Sid": "VendedLogDelivery",
        "Effect": "Allow",
        "Principal": {"Service": "delivery.logs.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group_arn(scope, name)}:log-stream:*",
        "Condition": {"StringEquals": {"aws:SourceAccount": stack.account}, "ArnLike": {"aws:SourceArn": source_arn}},
    }
    return _service_log_group(
        scope,
        id,
        name=name,
        statement=statement,
        retention=retention,
        removal_policy=removal_policy,
        log_group_class=log_group_class,
    )


def rule_log_group(
    scope: Construct,
    id: str,
    *,
    name: str,
    rule_name: str,
    retention: logs.RetentionDays,
    removal_policy: RemovalPolicy,
) -> logs.LogGroup:
    """A log group that the default-bus EventBridge rule ``rule_name`` may write matched events to (verified with
    MediaConvert job events, 2026-10-16).

    The rule ARN is built from its name, so the policy does not reference the rule, which references this group.
    """
    stack = Stack.of(scope)
    statement = {
        "Sid": "EventBridgeRuleTarget",
        "Effect": "Allow",
        "Principal": {"Service": "events.amazonaws.com"},
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"{log_group_arn(scope, name)}:*",
        "Condition": {
            "StringEquals": {"aws:SourceAccount": stack.account},
            "ArnEquals": {
                "aws:SourceArn": stack.format_arn(service="events", resource="rule", resource_name=rule_name)
            },
        },
    }
    return _service_log_group(
        scope,
        id,
        name=name,
        statement=statement,
        retention=retention,
        removal_policy=removal_policy,
        log_group_class=logs.LogGroupClass.STANDARD,
    )


# jsii calls bind(): its annotation types are runtime imports.
@jsii.implements(events.IRuleTarget)
class _LogGroupTarget:
    """Rule target delivering events to a log group whose own resource policy admits the rule.
    ``targets.CloudWatchLogGroup`` would add an account-level resource policy through a custom resource."""

    def __init__(self, log_group: logs.ILogGroup) -> None:
        self._log_group = log_group

    def bind(self, rule: IRuleRef, id: str | None = None) -> events.RuleTargetConfig:
        return events.RuleTargetConfig(arn=log_group_arn(self._log_group, self._log_group.log_group_name))


def alert_topic(scope: Construct, id: str, *, name: str, email: str | None) -> sns.Topic:
    """SNS topic of alarms and failure events, optionally with an email subscription (confirmed by the
    recipient)."""
    topic = sns.Topic(scope, id, topic_name=name, display_name="FAST Channels alerts", enforce_ssl=True)
    nag.acknowledge(
        topic,
        {
            _SNS_SSE: "Alarm and event notifications carry no secrets; CloudWatch alarms cannot publish to topics "
            "encrypted with the AWS managed key, and a customer managed key would cost more than it protects"
        },
    )
    if email:
        topic.add_subscription(subscriptions.EmailSubscription(email))
        nag.acknowledge(
            topic,
            {_SNS_REDRIVE: "Email delivery: alarm states and the event archive remain the record of a lost email"},
        )
    return topic


def cdn_error_alarm(
    scope: Construct, id: str, *, distribution: cf.IDistribution, topic: sns.ITopic, name: str
) -> cw.Alarm:
    """Alarm on the distribution's 5xx error rate. CloudFront publishes metrics in us-east-1 only, and an alarm
    must be in its metric's region: create it in a us-east-1 stack (Media there, otherwise Edge)."""
    alarm = cw.Alarm(
        scope,
        id,
        alarm_name=name,
        alarm_description="CloudFront 5xx responses above 5 % (MediaTailor or origin failures reach viewers)",
        metric=cdn_metric(distribution, "5xxErrorRate", statistic=cw.Stats.AVERAGE),
        threshold=5,
        evaluation_periods=3,
        datapoints_to_alarm=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    _notify(alarm, topic)
    return alarm


def cdn_metric(distribution: cf.IDistribution, name: str, *, statistic: str) -> cw.Metric:
    """A CloudFront distribution metric (published in us-east-1 for the ``Global`` region)."""
    return cw.Metric(
        namespace="AWS/CloudFront",
        metric_name=name,
        dimensions_map={"DistributionId": distribution.distribution_id, "Region": "Global"},
        statistic=statistic,
        period=_PERIOD,
        region=CLOUDFRONT_REGION,
    )


class FastEventBus(Construct):
    """The deployment's ``<prefix>-fast-channels`` EventBridge bus for domain events (``core.events``, source
    ``fast-channels.<prefix>``), with an archive for replays and INFO-level logs including event details
    delivered to ``/aws/vendedlogs/events/<prefix>-fast-channels``. Every other construct receives
    :attr:`bus` as ``event_bus``.

    INFO logs every ``PutEvents`` ingestion, rule match and target invocation, and every error (the ERROR level is
    included); EventBridge logs on a best-effort basis.

    Args:
        prefix: deployment prefix.
        log_retention: retention of the log group.
        archive_retention: how long the archive keeps events.
        removal_policy: what happens to the log group when the stack is deleted.

    Attributes:
        bus: the event bus.
        archive: archive of every event on the bus.
        log_group: the bus log group.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        archive_retention: Duration = DEFAULT_ARCHIVE_RETENTION,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        name = f"{prefix}-fast-channels"
        self.bus = events.EventBus(
            self,
            "Bus",
            event_bus_name=name,
            description=f"FAST Channels ({prefix}) domain events",
            log_config=events.LogConfig(level=events.Level.INFO, include_detail=events.IncludeDetail.FULL),
        )
        self.archive = self.bus.archive(
            "Archive",
            archive_name=name,
            description="Every FAST Channels event, for replay",
            event_pattern=events.EventPattern(account=[Stack.of(self).account]),
            retention=archive_retention,
        )
        self.log_group = vended_log_group(
            self,
            "Logs",
            name=f"/aws/vendedlogs/events/{name}",
            delivery_sources=f"{name}-*",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        source = logs.CfnDeliverySource(
            self, "LogSource", name=f"{name}-info", log_type="INFO_LOGS", resource_arn=self.bus.event_bus_arn
        )
        destination = logs.CfnDeliveryDestination(
            self,
            "LogDestination",
            name=f"{name}-logs",
            destination_resource_arn=log_group_arn(self, self.log_group.log_group_name),
            output_format="json",
        )
        logs.CfnDelivery(
            self, "LogDelivery", delivery_source_name=source.ref, delivery_destination_arn=destination.attr_arn
        )


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
        "coalesce(eventType, `cs-uri-stem`, message_type) as step, coalesce(adsRequestUrl, `cs-uri-query`, message) "
        "as detail",
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
"""Trace queries of SPEC §9; ``scripts/trace.py`` runs them."""


def census_definition(prefix: str) -> dict[str, Any]:
    """Express workflow counting this deployment's running Channel Assembly channels and publishing the count as
    ``FastChannels/RunningChannels{App=<prefix>}``.

    Channel Assembly publishes no metric while a channel runs without viewers, yet bills every running hour. A
    channel belongs to the deployment by its ``fast:app`` tag (IaC channels get it from the stage, agent channels
    from the tools; CONTRACTS §E): name prefixes would also match deployments whose prefix extends this one.
    """
    own = f"Tags.`fast:app` = '{prefix}'"
    return {
        "Comment": f"Count the running channels tagged fast:app={prefix}",
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


class ChannelCensus(Construct):
    """Runs :func:`census_definition` every :data:`CENSUS_INTERVAL` (EventBridge Scheduler, no retries: the next
    run follows shortly).

    Attributes:
        state_machine: the census workflow.
        running_channels: its metric (maximum per period).
        channel_hours: running channel-hours per day (the census samples summed over a day).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, id)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-channel-census",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_type=sfn.StateMachineType.EXPRESS,
            definition_body=sfn.DefinitionBody.from_string(json.dumps(census_definition(prefix))),
            comment=f"Publishes the number of running channels tagged fast:app={prefix}",
            timeout=Duration.minutes(1),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
        )
        self.state_machine.add_to_role_policy(
            iam.PolicyStatement(actions=["mediatailor:ListChannels"], resources=["*"])
        )
        self.state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": METRICS_NAMESPACE}},
            )
        )
        nag.acknowledge(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "mediatailor:ListChannels and cloudwatch:PutMetricData have no resource-level "
                "permissions (the latter is limited to the FastChannels namespace); X-Ray tracing needs '*'"
            },
        )
        scheduler.Schedule(
            self,
            "Schedule",
            description=f"Count the running channels tagged fast:app={prefix}",
            schedule=scheduler.ScheduleExpression.rate(CENSUS_INTERVAL),
            target=scheduler_targets.StepFunctionsStartExecution(self.state_machine, retry_attempts=0),
        )
        self.running_channels = cw.Metric(
            namespace=METRICS_NAMESPACE,
            metric_name=RUNNING_CHANNELS_METRIC,
            dimensions_map={"App": prefix},
            statistic=cw.Stats.MAXIMUM,
            period=_PERIOD,
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
    - Alarms: failed executions of each state machine, MediaConvert job errors on the queue, per channel the
      Channel Assembly errors and manifest requests at :data:`QUOTA_ALARM_RATIO` of
      ``channel_manifest_tps_quota``, per playback configuration ADS errors, timeouts and fill rate, running
      channel-hours per day (:class:`ChannelCensus`) and, when this stack is in us-east-1, the CloudFront 5xx rate.
    - Asset Failed, Ladder Failed and failed Autopilot Run events on ``event_bus`` are forwarded to the topic.
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
        channel_manifest_tps_quota: the account's "Channel Manifest Requests" quota.
        max_channel_hours_per_day: running channel-hours per day that raise the cost alarm.

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
        channel_manifest_tps_quota: int = CHANNEL_MANIFEST_TPS_QUOTA,
        max_channel_hours_per_day: float = 24,
    ) -> None:
        super().__init__(scope, id)
        self._prefix = prefix
        stack = Stack.of(self)
        self.topic = alert_topic(self, "Topic", name=f"{prefix}-alerts", email=notification_email)
        self.mediaconvert_log_group = self._mediaconvert_job_logs(log_retention, removal_policy)
        self._forward_failures(event_bus.bus)
        self.census = ChannelCensus(
            self, "Census", prefix=prefix, log_retention=log_retention, removal_policy=removal_policy
        )
        self.alarms: dict[str, cw.Alarm] = {}
        for label, state_machine in state_machines.items():
            self._alarm(
                f"{prefix} {label} failed",
                state_machine.metric_failed(period=_PERIOD, statistic=cw.Stats.SUM),
                threshold=1,
                description=f"A {label} execution failed",
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
            ),
        )
        CfnOutput(self, "DashboardName", key="DashboardName", value=self.dashboard.dashboard_name)

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
        alarm = cw.Alarm(
            self,
            "".join(part.capitalize() for part in name.removeprefix(f"{self._prefix}-").split("-")) + "Alarm",
            alarm_name=name,
            alarm_description=description,
            metric=metric,
            threshold=threshold,
            evaluation_periods=evaluation_periods,
            comparison_operator=comparison,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        _notify(alarm, self.topic)
        self.alarms[name] = alarm

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
            targets=[_LogGroupTarget(log_group)],
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
                targets=[events_targets.SnsTopic(self.topic, message=message)],
            )
            nag.acknowledge(rule, {_RULE_DLQ: "Notification: EventBridge retries for 24 h; the bus archive keeps it"})


def _notify(alarm: cw.Alarm, topic: sns.ITopic) -> None:
    action = cw_actions.SnsAction(topic)
    alarm.add_alarm_action(action)
    alarm.add_ok_action(action)


def _mediaconvert_metrics(queue_arn: str) -> dict[str, cw.Metric]:
    return {
        name: cw.Metric(
            namespace="AWS/MediaConvert",
            metric_name=name,
            dimensions_map={"Queue": queue_arn},
            statistic=cw.Stats.SUM,
            period=_PERIOD,
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
            period=_PERIOD,
        )

    e4, e5 = _metric_id("e4", channel), _metric_id("e5", channel)
    errors = cw.MathExpression(
        expression=f"FILL({e4}, 0) + FILL({e5}, 0)",
        using_metrics={e4: metric("4xxErrorCount"), e5: metric("5xxErrorCount")},
        period=_PERIOD,
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
            period=_PERIOD,
        )

    errors, timeouts = _metric_id("errors", configuration), _metric_id("timeouts", configuration)
    return {
        "errors": cw.MathExpression(
            expression=f"FILL({errors}, 0) + FILL({timeouts}, 0)",
            using_metrics={
                errors: metric("AdDecisionServer.Errors", cw.Stats.SUM),
                timeouts: metric("AdDecisionServer.Timeouts", cw.Stats.SUM),
            },
            period=_PERIOD,
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
) -> list[list[cw.IWidget]]:
    def graph(title: str, left: Sequence[cw.IMetric], **options: Any) -> cw.GraphWidget:
        return cw.GraphWidget(title=title, left=list(left), width=8, height=6, **options)

    quota = cw.HorizontalAnnotation(
        value=QUOTA_ALARM_RATIO * CHANNEL_MANIFEST_TPS_QUOTA * 300, label="80 % of the default quota"
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
                        sm.metric_succeeded(period=_PERIOD, label=f"{label} succeeded"),
                        sm.metric_failed(period=_PERIOD, label=f"{label} failed"),
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
