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
- **A ratio, not a difference, over enough markers.** A session that joins during a break asks for what is left of
  it (avail suppression ``AFTER_LIVE_EDGE`` with ``PARTIAL_AVAIL``, ADR-0021: measured 30 s and 76 s into a 120 s
  break); whether one that joins in a break's last segments asks at all was not measured. With a large audience such
  joiners are in every break, so a count could alarm, and a minute of a few markers alone (a probe session, a tester)
  is not a loss. A sibling with fewer sessions is covered by the smoke test and the hourly zero-ad-sessions detector.
- **Two lossy minutes.** A session's marker and its request are logged milliseconds apart (P0-20 evidence); one that
  straddles a minute makes that minute lossy and the next one not, so it never alarms alone. Breaks minutes apart
  still alarm at the second lossy break.
- Prefetch, which requests ads before the marker, is not used by the siblings yet; it would need its own expression.

Per sibling, too, an alarm on the ad decision server's errors (:class:`AdsErrorAlarms`): a route that answers
every request with an error (a missing stored request, Prebid Server down, the origin gate refusing) plays the slate
and raises no other alarm.
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
ADS_ERRORS_PERIOD: Final = Duration.minutes(15)
ADS_ERRORS_PERIODS: Final = 2
"""Consecutive :data:`ADS_ERRORS_PERIOD` periods, each with more than :data:`ADS_ERRORS_RATIO` of its requests
failed, that raise the ADS errors alarm."""
ADS_ERRORS_RATIO: Final = 0.5
ADS_ERRORS_MIN_REQUESTS: Final = 5
"""ADS requests a period needs to be judged."""


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


def _mediatailor_metric(name: str, configuration: str, statistic: str) -> cw.Metric:
    """``AWS/MediaTailor`` metric ``name`` of ``configuration``, per :data:`ADS_ERRORS_PERIOD`."""
    return cw.Metric(
        namespace="AWS/MediaTailor",
        metric_name=name,
        dimensions_map={"ConfigurationName": configuration},
        statistic=statistic,
        period=ADS_ERRORS_PERIOD,
    )


class AdsErrorAlarms(Construct):
    """An alarm ``<configuration>-ads-errors`` per sibling: more than :data:`ADS_ERRORS_RATIO` of its ADS requests
    failed in :data:`ADS_ERRORS_PERIODS` consecutive 15-minute periods of at least :data:`ADS_ERRORS_MIN_REQUESTS`.

    - **The ratio.** ``AdDecisionServer.Errors`` counts non-200, empty and timed-out answers. An empty VAST (no
      fill) is not one: on the rig ``fastcie-exp-mbj`` (2026-10-30), 2 ``EMPTY_VAST_RESPONSE`` events counted 0.
      ``AdDecisionServer.Latency`` has a sample per request, a failed one or a prefetch retrieval included. On
      fastm (2026-10-30) the samples matched ``MAKING_ADS_REQUEST`` minute by minute: 07:41 to 07:46Z on
      fastm-demo-p-a, 8 of them refused with 403 and counted in both, and 14:13 to 14:18Z on fastm-demo-p-b, prefetch
      retrievals included.
    - **The noise.** A mid-break joiner's remainder request (ADR-0021) is refused with 400 when it is off the
      30 s grid, as long as Prebid Server has a stored request per grid duration. Those requests come in the two
      minutes of a break. A period that holds only them is followed by a period with the next break's requests, which
      succeed, or with too few to judge. So two failing periods in a row mean a broken route, which every request
      of a break reports.
    - **Outside the candidate alarm.** A pointer rollback or a remap to the last good tree does not mend Prebid
      Server, its stored requests or the origin gate. And a failing stable sibling would roll back the deployment
      that moves viewers off it. The smoke test judges a candidate's fill before any viewer moves.

    Args:
        siblings: the sibling configurations.
        topic: the stage's alert topic.

    Attributes:
        alarms: by configuration.
    """

    def __init__(self, scope: Construct, id: str, *, siblings: Sequence[str], topic: sns.ITopic) -> None:
        super().__init__(scope, id)
        self.alarms: dict[str, cw.Alarm] = {}
        for sibling in siblings:
            self.alarms[sibling] = alarm(
                self,
                f"{sibling}-ads-errors".replace("-", " ").title().replace(" ", ""),
                name=names.ads_errors_alarm(sibling),
                metric=cw.MathExpression(
                    expression=f"IF(FILL(requests, 0) >= {ADS_ERRORS_MIN_REQUESTS}, "
                    "FILL(errors, 0) / FILL(requests, 0), 0)",
                    using_metrics={
                        "errors": _mediatailor_metric("AdDecisionServer.Errors", sibling, cw.Stats.SUM),
                        "requests": _mediatailor_metric("AdDecisionServer.Latency", sibling, cw.Stats.SAMPLE_COUNT),
                    },
                    period=ADS_ERRORS_PERIOD,
                    label=f"{sibling}: share of ADS requests that failed",
                ),
                threshold=ADS_ERRORS_RATIO,
                comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=ADS_ERRORS_PERIODS,
                datapoints_to_alarm=ADS_ERRORS_PERIODS,
                description=f"{sibling}: most ad decision server requests failed (non-200, empty or timed out) for "
                "30 minutes: a missing stored request, Prebid Server or the origin gate; the breaks play the slate",
                topic=topic,
            )


class LogReconciler(Construct):
    """Every hour, ``fast_channels.functions.release.reconcile`` compares each sibling's ADS log with
    ``AWS/MediaTailor``'s filled ad time and publishes what the log is missing (``UnloggedFilledSeconds``); the alarm
    ``<prefix>-release-log-loss`` fires on a second or more. A gap under-counts delivery, which reads the same log's
    beacons.

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
                metric_name="UnloggedFilledSeconds",
                dimensions_map={"service": "release.reconcile"},
                statistic=cw.Stats.SUM,
                period=Duration.hours(1),
            ),
            threshold=1,
            description="MediaTailor filled ad time its vended ADS log does not have (Avail.FilledDuration against "
            "FILLED_AVAIL): delivery, which counts that log's beacons, is under-counted for those breaks (see the "
            "function's log for the siblings and the hour)",
            topic=topic,
        )
