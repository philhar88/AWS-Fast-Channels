"""``ModuleAlarms``: the share of breaks in which the fast-ctv module fell back, from Prebid Server's container log.

The module logs one ``fast-ctv-pod`` line per auction: ``policy_fallback`` names why the ``/policy`` patch was not
applied (``POLICY_UNAVAILABLE``, ``POLICY_HTTP_<status>``, ``NO_POLICY_HOOK``…, null when it was), and ``select``
starts with ``SELECT_FALLBACK`` when the pod is the auction's house ads instead of ``/select``'s answer. Metric filters
count the lines, the policy fallbacks and the select fallbacks, leaving out the warm-up's and the herd's synthetic
auctions (``warmup-<n>`` sessions on ``<prefix>-warmup``), which run on cold tasks by design. Two alarms watch the
shares over 5 minutes, once at least :data:`MIN_PODS` auctions ran:

- ``<prefix>-pds-policy-fallbacks``: more than 1% of breaks served without the policy (own book only, no viewer data);
- ``<prefix>-pds-select-fallbacks``: more than 0.5% of pods built by the module from house bids.

The module's log line is not JSON as a whole (Prebid Server's log line embeds it unescaped), so the filters match
terms.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.infra.constructs.alerts import alarm

if TYPE_CHECKING:
    from aws_cdk import aws_sns as sns

MIN_PODS: Final = 100
"""Auctions in 5 minutes below which the shares are not judged."""
PERIOD: Final = Duration.minutes(5)
POLICY_SHARE: Final = 1.0
SELECT_SHARE: Final = 0.5
_POD: Final = '"fast-ctv-pod" -"warmup"'
PATTERNS: Final = {
    "Pods": _POD,
    "PolicyFallbacks": f'{_POD} "POLICY_"',
    "SelectFallbacks": f'{_POD} "SELECT_FALLBACK"',
}
"""Metric name -> the filter pattern of the module's lines it counts."""


def namespace(prefix: str) -> str:
    """The metrics' namespace: one per stage, since a metric filter cannot add a dimension of its own."""
    return f"FastChannels/PrebidServer/{prefix}"


class ModuleAlarms(Construct):
    """The fallback metrics and alarms (module docstring).

    Args:
        prefix: the stage's prefix.
        log_group_name: Prebid Server's container log group (the guidance's).
        alert_topic: where the alarms notify.

    Attributes:
        filters: the metric filters by metric name. policy, select: the alarms.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str, log_group_name: str, alert_topic: sns.ITopic) -> None:
        super().__init__(scope, id)
        group = logs.LogGroup.from_log_group_name(self, "PrebidServerLogs", log_group_name)
        self.filters = {
            name: logs.MetricFilter(
                self,
                name,
                log_group=group,
                filter_pattern=logs.FilterPattern.literal(pattern),
                metric_namespace=namespace(prefix),
                metric_name=name,
                metric_value="1",
                unit=cw.Unit.COUNT,
            )
            for name, pattern in PATTERNS.items()
        }
        metrics = {
            name: cw.Metric(namespace=namespace(prefix), metric_name=name, statistic=cw.Stats.SUM, period=PERIOD)
            for name in PATTERNS
        }

        def share(fallbacks: str, label: str) -> cw.MathExpression:
            return cw.MathExpression(
                expression=f"IF(pods >= {MIN_PODS}, 100 * FILL(fallbacks, 0) / pods, 0)",
                using_metrics={"pods": metrics["Pods"], "fallbacks": metrics[fallbacks]},
                label=label,
                period=PERIOD,
            )

        self.policy = alarm(
            self,
            "PolicyFallbacksAlarm",
            name=f"{prefix}-pds-policy-fallbacks",
            metric=share("PolicyFallbacks", "Breaks without the /policy patch, %"),
            threshold=POLICY_SHARE,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            description=f"More than {POLICY_SHARE}% of breaks in 5 minutes were served without the PDS's /policy "
            "patch (own book only, no third party, no viewer data): the PDS is slow, failing or unreachable",
            topic=alert_topic,
        )
        self.select = alarm(
            self,
            "SelectFallbacksAlarm",
            name=f"{prefix}-pds-select-fallbacks",
            metric=share("SelectFallbacks", "Pods from the module's house fallback, %"),
            threshold=SELECT_SHARE,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            description=f"More than {SELECT_SHARE}% of pods in 5 minutes were the module's house fallback instead of "
            "the PDS's /select answer: no paid ad and no reservation in those breaks",
            topic=alert_topic,
        )
