"""``RealTimeBurst``: names the cause when the PDS's policy fallbacks come from a real-time herd on a prefetched
sibling (ADR-0036).

With prefetch, MediaTailor asks for a break ahead of it, spread over the shaping window, and only late joiners ask in
real time. When it cannot use a retrieval (a schedule deleted after its retrievals, fixed in M3-06's #82) or a break
has no schedule (published closer than the retrieval lead), it asks for every session at the marker, all at once. A
sub-second herd of about 50 auctions a task outruns the task's 2 vCPU (measured: 64-71 of 100 auctions fell back in
0.49 s; 0 at 25 auctions/s per task), and the module serves those breaks SAFE.

- A metric filter on the siblings' log group counts ``MAKING_ADS_REQUEST`` events with no ``prefetchScheduleName``
  per configuration (``RealTimeAdsRequests``, beside the marker detector's ``AdsRequests``).
- One alarm per prefetched sibling, ``<sibling>-real-time-requests``, without actions: more than
  :data:`REAL_TIME_SHARE` % of its ad requests in 5 minutes were real time, over at least :data:`MIN_REQUESTS`.
- The composite ``<prefix>-pds-burst-real-time`` notifies when the PDS's ``<prefix>-pds-policy-fallbacks`` (more than
  1% of breaks without the policy patch in 5 minutes, the Pds stack's) is in ALARM together with any of them.

It is an approximation of "a real-time request for a break that had a schedule": the exact join needs the schedules
and the log, like the hourly reconciler, too late for an alarm. A break published inside the retrieval lead is real
time by design, and the composite says so.
"""

from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from fast_channels.core.monetization import names
from fast_channels.core.pds.names import policy_fallbacks_alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE

REAL_TIME_SHARE: Final = 50.0
"""The share of a prefetched sibling's ad requests in 5 minutes, in %, above which they count as a real-time herd:
with prefetch working, only late joiners and mid-break joiners ask in real time."""
MIN_REQUESTS: Final = 20
"""Ad requests in 5 minutes below which the share is not judged (the PDS's ``MIN_PODS``)."""
PERIOD: Final = Duration.minutes(5)
REAL_TIME: Final = logs.FilterPattern.all(
    logs.FilterPattern.string_value("$.eventType", "=", "MAKING_ADS_REQUEST"),
    logs.FilterPattern.not_exists("$.prefetchScheduleName"),
)
"""An ad request MediaTailor made at the break, not a prefetch retrieval."""


class RealTimeBurst(Construct):
    """The real-time request metric, the per-sibling alarms and the composite (module docstring).

    Args:
        prefix: the stage's prefix.
        log_group: the siblings' MediaTailor log group.
        siblings: the prefetched siblings (``modules.release.prefetch.siblings``).
        topic: the stage's alert topic, which the composite notifies.

    Attributes:
        alarms: the per-sibling alarms by configuration. composite: the composite alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_group: logs.ILogGroup,
        siblings: Sequence[str],
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, id)
        logs.MetricFilter(
            self,
            names.REAL_TIME_REQUESTS,
            log_group=log_group,
            filter_name=names.REAL_TIME_REQUESTS,
            filter_pattern=REAL_TIME,
            metric_namespace=METRICS_NAMESPACE,
            metric_name=names.REAL_TIME_REQUESTS,
            metric_value="1",
            dimensions={"ConfigurationName": "$.originId"},
            unit=cw.Unit.COUNT,
        )

        def metric(name: str, configuration: str) -> cw.Metric:
            return cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name=name,
                dimensions_map={"ConfigurationName": configuration},
                statistic=cw.Stats.SUM,
                period=PERIOD,
            )

        self.alarms: dict[str, cw.Alarm] = {}
        for sibling in siblings:
            self.alarms[sibling] = cw.Alarm(
                self,
                f"{sibling}-real-time".replace("-", " ").title().replace(" ", ""),
                alarm_name=names.real_time_alarm(sibling),
                alarm_description=f"{sibling}: more than {REAL_TIME_SHARE:g}% of its ad requests in 5 minutes were "
                "real time, not prefetched (input of the burst composite; no notification of its own)",
                metric=cw.MathExpression(
                    expression=f"IF(FILL(requests, 0) >= {MIN_REQUESTS}, 100 * FILL(realtime, 0) / requests, 0)",
                    using_metrics={
                        "requests": metric(names.MARKER_METRICS["MAKING_ADS_REQUEST"], sibling),
                        "realtime": metric(names.REAL_TIME_REQUESTS, sibling),
                    },
                    period=PERIOD,
                    label=f"{sibling}: real-time share of ad requests, %",
                ),
                threshold=REAL_TIME_SHARE,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
        fallbacks = cw.Alarm.from_alarm_name(self, "PolicyFallbacks", policy_fallbacks_alarm(prefix))
        self.composite = cw.CompositeAlarm(
            self,
            "Composite",
            composite_alarm_name=names.burst_alarm(prefix),
            alarm_description="The PDS's policy fallbacks passed 1% of breaks while most of a prefetched sibling's ad "
            "requests were real time: MediaTailor asked for every session at the marker at once (a break it could not "
            "prefetch, or a retrieval it did not use), a sub-second herd the tasks' CPU cannot absorb (ADR-0036). "
            "Those breaks were served SAFE. Check the break's prefetch schedule (pf-<evid>; CloudTrail "
            "Create/DeletePrefetchSchedule) and its FILLED_AVAIL.prefetchScheduleName",
            alarm_rule=cw.AlarmRule.all_of(
                cw.AlarmRule.from_alarm(fallbacks, cw.AlarmState.ALARM),
                cw.AlarmRule.any_of(*(cw.AlarmRule.from_alarm(a, cw.AlarmState.ALARM) for a in self.alarms.values())),
            ),
        )
        action = cw_actions.SnsAction(topic)
        self.composite.add_alarm_action(action)
        self.composite.add_ok_action(action)
