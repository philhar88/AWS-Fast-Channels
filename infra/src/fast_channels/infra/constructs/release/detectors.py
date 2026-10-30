"""Detector 1 of DESIGN §18.3, per sibling: ad markers with no ad request.

MediaTailor drops an ad request whose rendered body is not valid JSON (an unquoted unresolved variable, a ``"`` or a
``\\`` in a player value) and logs nothing but the break's ``AD_MARKER_FOUND``: no error, no
``WARNING_NO_ADVERTISEMENTS``, an unfilled break (P0-20 §4.14). A PRE_ADS budget exhausted before the request looks
the same (P0-03). Neither raises ``AdDecisionServer.Errors``. Three metric filters on the siblings' log group count
``AD_MARKER_FOUND``, ``MAKING_ADS_REQUEST`` and ``WARNING_NO_ADVERTISEMENTS`` per configuration (``originId``), and
one alarm per configuration fires when, in :data:`LOSSY_MINUTES` of :data:`WINDOW_MINUTES` minutes with at least
:data:`MIN_MARKERS` markers, more than :data:`LOSS_RATIO` of them had no request.

- **One marker per break.** On a Channel Assembly channel MediaTailor logs ``AD_MARKER_FOUND`` twice per break and
  session: when the ``EXT-X-DATERANGE`` appears, then again (a new ``adBreakCorrelationId``, no request) when it gains
  its ``DURATION`` at the break's end (fastm, 2026-10-29). Only the first is counted: :data:`BREAK_START` leaves out
  markers that carry ``,DURATION=``. A VOD ``EXT-X-CUE-OUT`` is counted.
- **A ratio, not a difference, over enough markers.** A session that joins during a break sees its marker but
  gets no request (avail suppression ``BEHIND_LIVE_EDGE``); with a large audience such joiners are in every break, so
  a count would alarm, and a minute of a few joiners alone (a probe session, a tester) is not a loss. A sibling with
  fewer sessions is covered by the smoke test and the hourly zero-ad-sessions detector.
- **Two lossy minutes.** A session's marker and its request are logged milliseconds apart (P0-20 evidence); one that
  straddles a minute makes that minute lossy and the next one not, so it never alarms alone. Breaks minutes apart
  still alarm at the second lossy break.
- Prefetch, which requests ads before the marker, is not used by the siblings yet; it would need its own expression.
"""

from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from constructs import Construct

from fast_channels.core.monetization import names
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE, FastFunction

EVENTS: Final = names.MARKER_METRICS
"""ADS log event type -> metric (namespace ``FastChannels``, dimension ``ConfigurationName``)."""
BREAK_START: Final = logs.FilterPattern.all(
    logs.FilterPattern.string_value("$.eventType", "=", "AD_MARKER_FOUND"),
    logs.FilterPattern.string_value("$.adMarkers", "!=", "*,DURATION=*"),
)
"""A break's first marker: not the ``EXT-X-DATERANGE`` that gained its duration when the break ended."""
PERIOD: Final = Duration.minutes(1)
WINDOW_MINUTES: Final = 10
LOSSY_MINUTES: Final = 2
"""Lossy minutes, out of :data:`WINDOW_MINUTES`, that raise the alarm."""
LOSS_RATIO: Final = 0.5
"""A minute is lossy when more than this share of its markers had no request (an invalid body: all of them)."""
MIN_MARKERS: Final = 5
"""Markers a minute needs to be judged."""


def sibling_metric(name: str, configuration: str, statistic: str = cw.Stats.SUM) -> cw.Metric:
    """One of :data:`EVENTS`' metrics for ``configuration``."""
    return cw.Metric(
        namespace=METRICS_NAMESPACE,
        metric_name=name,
        dimensions_map={"ConfigurationName": configuration},
        statistic=statistic,
        period=PERIOD,
    )


class MarkerDetector(Construct):
    """The metric filters on ``log_group`` and an alarm ``<configuration>-markers-without-ads-request`` per
    configuration.

    Args:
        log_group: the siblings' MediaTailor log group (the release module's).
        siblings: the configurations watched: the siblings, and the lab's detector check (``names.lab_check``).
        topic: the stage's alert topic.

    Attributes:
        alarms: by configuration.
    """

    def __init__(
        self, scope: Construct, id: str, *, log_group: logs.ILogGroup, siblings: Sequence[str], topic: sns.ITopic
    ) -> None:
        super().__init__(scope, id)
        for event, metric in EVENTS.items():
            logs.MetricFilter(
                self,
                metric,
                log_group=log_group,
                filter_name=f"{metric}",
                filter_pattern=BREAK_START
                if event == "AD_MARKER_FOUND"
                else logs.FilterPattern.string_value("$.eventType", "=", event),
                metric_namespace=METRICS_NAMESPACE,
                metric_name=metric,
                metric_value="1",
                dimensions={"ConfigurationName": "$.originId"},
                unit=cw.Unit.COUNT,
            )
        self.alarms: dict[str, cw.Alarm] = {}
        for sibling in siblings:
            markers = sibling_metric(EVENTS["AD_MARKER_FOUND"], sibling)
            requests = sibling_metric(EVENTS["MAKING_ADS_REQUEST"], sibling)
            self.alarms[sibling] = alarm(
                self,
                f"{sibling}-markers".replace("-", " ").title().replace(" ", ""),
                name=names.markers_alarm(sibling),
                metric=cw.MathExpression(
                    expression=f"IF(FILL(markers, 0) >= {MIN_MARKERS}, "
                    "(FILL(markers, 0) - FILL(requests, 0)) / FILL(markers, 0), 0)",
                    using_metrics={"markers": markers, "requests": requests},
                    period=PERIOD,
                    label=f"{sibling}: share of ad markers without an ad request",
                ),
                threshold=LOSS_RATIO,
                comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=WINDOW_MINUTES,
                datapoints_to_alarm=LOSSY_MINUTES,
                description=f"{sibling}: breaks saw ad markers but MediaTailor made no ad request (an invalid "
                "rendered body or an exhausted PRE_ADS budget): the breaks play unfilled",
                topic=topic,
            )


class LogReconciler(Construct):
    """Every hour, ``fast_channels.functions.release.reconcile`` compares each sibling's ADS log with
    ``AWS/MediaTailor``'s counts and publishes the ad requests the log is missing (``UnloggedAdsRequests``); the alarm
    ``<prefix>-release-log-loss`` fires on any. A gap under-counts delivery, which reads the same log's beacons.

    Args:
        prefix: deployment prefix.
        siblings: the sibling configurations.
        environment: the Release Controller's function environment (the prefix, the release table).
        topic: the stage's alert topic.
        log_retention: of the function's logs.

    Attributes:
        function: the reconciliation. alarm: its alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        siblings: Sequence[str],
        environment: dict[str, str],
        topic: sns.ITopic,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Function",
            handler="fast_channels.functions.release.reconcile.handler",
            description="Release: ad requests MediaTailor made but left out of the siblings' ADS log (hourly)",
            environment=environment | {"RELEASE_SIBLINGS": ",".join(siblings)},
            timeout=Duration.minutes(5),
            memory_size=256,
            log_retention=log_retention,
        )
        self.function.allow(["cloudwatch:GetMetricData", "logs:GetQueryResults"], ["*"])  # no resource-level
        self.function.allow(
            ["logs:DescribeLogGroups"],
            [arn(self, "logs", "log-group", "*", colon=True)],
            why="logs:DescribeLogGroups has no resource-level permissions (the siblings' group's creation time)",
        )
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: "Invoked by an hourly schedule; the next run repeats it"})
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: run the release log reconciliation",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(f"{self.function.function.function_arn}:*"): "Scheduler L2 grant: the versions"},
        )
        scheduler.Schedule(
            self,
            "Schedule",
            description=f"Release log reconciliation of {prefix} every hour",
            schedule=scheduler.ScheduleExpression.rate(Duration.hours(1)),
            target=scheduler_targets.LambdaInvoke(
                self.function.function,
                input=scheduler.ScheduleTargetInput.from_object({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                retry_attempts=1,
                role=role,
            ),
        )
        self.alarm = alarm(
            self,
            "Alarm",
            name=f"{prefix}-release-log-loss",
            metric=cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="UnloggedAdsRequests",
                dimensions_map={"service": "release.reconcile"},
                statistic=cw.Stats.SUM,
                period=Duration.hours(1),
            ),
            threshold=1,
            description="MediaTailor made ad requests its vended ADS log does not have: delivery, which counts that "
            "log's beacons, is under-counted for those breaks (see the function's log for the siblings and the hour)",
            topic=topic,
        )
