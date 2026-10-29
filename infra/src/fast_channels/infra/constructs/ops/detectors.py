"""The ops module's alarms and detectors: account quota alarms, hook alarms per IaC playback configuration, the
zero-ad-sessions detector and the Channel Assembly alerts poller.

Every alarm treats missing data as not breaching (new or idle resources publish nothing) and notifies the stage's
alert topic on ALARM and OK.
"""

from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import PERIOD, alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE, FastFunction
from fast_channels.infra.constructs.logs import log_group_arn

__all__ = [
    "AD_INSERTION_REQUESTS_PER_SECOND",
    "CHANNEL_ASSEMBLY_REQUESTS_PER_SECOND",
    "HOOK_METRICS",
    "QUOTA_ALARM_RATIO",
    "AccountQuotaAlarms",
    "AlertsPoller",
    "ConfigurationAlarms",
    "ZeroAdsDetector",
]

QUOTA_ALARM_RATIO: Final = 0.8
AD_INSERTION_REQUESTS_PER_SECOND: Final = 10_000
"""MediaTailor's account quota of ad insertion requests per second (``L-32FBC51C``, default); it has no usage metric,
so the alarm sums every playback configuration's ``Requests``."""
CHANNEL_ASSEMBLY_REQUESTS_PER_SECOND: Final = 400
"""Channel Assembly's account quota of channel requests per second (default)."""
HOOK_METRICS: Final = ("PreSessionInitHook.Errors", "PreAdsRequestHook.Errors", "Function.Errors", "GetManifest.Errors")
"""Per playback configuration: any of these in five minutes raises the configuration's alarm."""
SCHEDULED_TIME: Final = "<aws.scheduler.scheduled-time>"


def _scheduled_input() -> scheduler.ScheduleTargetInput:
    return scheduler.ScheduleTargetInput.from_object({"scheduled_time": SCHEDULED_TIME})


def _schedule_role(scope: Construct, id: str, function: FastFunction) -> iam.Role:
    """A role EventBridge Scheduler assumes to invoke ``function`` (and its versions)."""
    role = iam.Role(scope, id, assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
    nag.acknowledge_references(
        role,
        {nag.wildcard_resource(f"{function.function.function_arn}:*"): "Scheduler L2 grant: the function's versions"},
    )
    return role


class AccountQuotaAlarms(Construct):
    """Alarms at :data:`QUOTA_ALARM_RATIO` of the account's ad insertion and Channel Assembly request quotas, summed
    over every playback configuration and channel of the account (CloudWatch Metrics Insights), for 3 minutes.

    Args:
        prefix: deployment prefix (alarm names).
        topic: the stage's alert topic.

    Attributes:
        alarms: by name.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str, topic: sns.ITopic) -> None:
        super().__init__(scope, id)
        minute = Duration.minutes(1)
        quotas = (
            (
                "ad-insertion-requests",
                'SELECT SUM(Requests) FROM SCHEMA("AWS/MediaTailor", ConfigurationName)',
                AD_INSERTION_REQUESTS_PER_SECOND,
                "MediaTailor ad insertion requests of every playback configuration in the account are above "
                f"{QUOTA_ALARM_RATIO:.0%} of the {AD_INSERTION_REQUESTS_PER_SECOND:,}/s quota (L-32FBC51C): request "
                "an increase in Service Quotas (runbook R7)",
            ),
            (
                "channel-assembly-requests",
                'SELECT SUM(RequestCount) FROM SCHEMA("AWS/MediaTailor", Channel)',
                CHANNEL_ASSEMBLY_REQUESTS_PER_SECOND,
                "Channel Assembly requests of every channel in the account are above "
                f"{QUOTA_ALARM_RATIO:.0%} of the {CHANNEL_ASSEMBLY_REQUESTS_PER_SECOND}/s quota: request an increase "
                "in Service Quotas (runbook R7)",
            ),
        )
        self.alarms: dict[str, cw.Alarm] = {}
        for what, query, per_second, description in quotas:
            name = f"{prefix}-account-{what}"
            self.alarms[name] = alarm(
                self,
                "".join(part.title() for part in what.split("-")) + "Alarm",
                name=name,
                metric=cw.MathExpression(expression=query, period=minute, label=f"{what} per minute"),
                threshold=QUOTA_ALARM_RATIO * per_second * minute.to_seconds(),
                evaluation_periods=3,
                description=description,
                topic=topic,
            )


class ConfigurationAlarms(Construct):
    """One alarm per playback configuration and :data:`HOOK_METRICS` metric: any error in five minutes.

    Args:
        configurations: playback configuration names (the IaC channels').
        topic: the stage's alert topic.

    Attributes:
        alarms: by name (``<configuration>-<metric, dotted name in kebab case>``).
    """

    def __init__(self, scope: Construct, id: str, *, configurations: Sequence[str], topic: sns.ITopic) -> None:
        super().__init__(scope, id)
        self.alarms: dict[str, cw.Alarm] = {}
        for configuration in configurations:
            for metric in HOOK_METRICS:
                slug = metric.replace(".", "-").lower()
                name = f"{configuration}-{slug}"
                self.alarms[name] = alarm(
                    self,
                    f"{configuration}-{slug}".replace("-", " ").title().replace(" ", ""),
                    name=name,
                    metric=cw.Metric(
                        namespace="AWS/MediaTailor",
                        metric_name=metric,
                        dimensions_map={"ConfigurationName": configuration},
                        statistic=cw.Stats.SUM,
                        period=PERIOD,
                    ),
                    threshold=1,
                    description=f"{configuration}: {metric} in the last five minutes",
                    topic=topic,
                )


class ZeroAdsDetector(Construct):
    """Every hour, ``fast_channels.functions.ops.zero_ads_sessions`` counts the sessions that saw at least three
    breaks without an ad request; the alarm ``<prefix>-zero-ads-sessions`` fires on any.

    Args:
        prefix: deployment prefix.
        ads_log_group: the deployment's MediaTailor vended-log group.
        topic: the stage's alert topic.
        log_retention: retention of the function's logs.
        removal_policy: what happens to them when the stack is deleted.

    Attributes:
        function: the detector. alarm: its alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        ads_log_group: str,
        topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Detector",
            handler="fast_channels.functions.ops.zero_ads_sessions.handler",
            description="Count the sessions that saw ad breaks without requesting ads (hourly)",
            environment={"ADS_LOG_GROUP": ads_log_group},
            timeout=Duration.minutes(5),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.function.allow(["logs:StartQuery"], [log_group_arn(self, ads_log_group)])
        self.function.allow(["logs:GetQueryResults"], ["*"])  # no resource-level permissions
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: "Invoked by an hourly schedule; the next run repeats it"})
        scheduler.Schedule(
            self,
            "Schedule",
            description=f"Count the zero-ad sessions of {prefix} every hour",
            schedule=scheduler.ScheduleExpression.rate(Duration.hours(1)),
            target=scheduler_targets.LambdaInvoke(
                self.function.function,
                input=_scheduled_input(),
                retry_attempts=1,
                role=_schedule_role(self, "ScheduleRole", self.function),
            ),
        )
        self.alarm = alarm(
            self,
            "Alarm",
            name=f"{prefix}-zero-ads-sessions",
            metric=cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="ZeroAdsSessions",
                dimensions_map={"service": "ops.zero_ads_sessions"},
                statistic=cw.Stats.SUM,
                period=Duration.hours(1),
            ),
            threshold=1,
            description="Sessions saw at least three ad breaks but requested no ads: possible ad suppression by a "
            "direct caller (see the detector's log for the session ids)",
            topic=topic,
        )


class AlertsPoller(Construct):
    """Every minute, ``fast_channels.functions.ops.list_alerts`` puts a ``Channel Alert`` event on the stage's bus
    for each Channel Assembly alert of a running channel that changed in that minute (nothing while the census
    counts no running channel).

    Args:
        prefix: deployment prefix.
        event_bus: the stage's bus.
        census_parameter: the census's last-count parameter.
        log_retention: retention of the function's logs.
        removal_policy: what happens to them when the stack is deleted.

    Attributes:
        function: the poller.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        event_bus: events.IEventBus,
        census_parameter: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Poller",
            handler="fast_channels.functions.ops.list_alerts.handler",
            description="Publish Channel Alert events for the alerts of running channels (every minute)",
            environment={
                "FAST_PREFIX": prefix,
                "EVENT_BUS": event_bus.event_bus_name,
                "CENSUS_PARAMETER": census_parameter,
            },
            timeout=Duration.seconds(50),
            memory_size=256,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.function.allow(["mediatailor:ListChannels"], ["*"])  # no resource-level permissions
        # ListAlerts is authorized on the account's alerts resource, not on the channel queried (observed on fastm:
        # "not authorized to perform: mediatailor:ListAlerts on resource: ...:alerts/*"); the ingest pipeline grants
        # it the same way.
        self.function.allow(
            ["mediatailor:ListAlerts"],
            [arn(self, "mediatailor", "alerts", "*")],
            why="ListAlerts is authorized on alerts/* only",
        )
        self.function.allow(["ssm:GetParameter"], [arn(self, "ssm", "parameter", census_parameter.lstrip("/"))])
        self.function.allow(["events:PutEvents"], [event_bus.event_bus_arn])
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: "Invoked by a schedule every minute; the next run repeats it"})
        scheduler.Schedule(
            self,
            "Schedule",
            description=f"Publish the alerts of {prefix}'s running channels every minute",
            schedule=scheduler.ScheduleExpression.rate(Duration.minutes(1)),
            target=scheduler_targets.LambdaInvoke(
                self.function.function,
                input=_scheduled_input(),
                retry_attempts=1,
                role=_schedule_role(self, "ScheduleRole", self.function),
            ),
        )
