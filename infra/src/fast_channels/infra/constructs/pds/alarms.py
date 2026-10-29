"""``ModuleAlarms``: the share of breaks in which the fast-ctv module fell back, from Prebid Server's container log.

The module logs one ``fast-ctv-pod`` line per auction: ``policy_fallback`` names why the ``/policy`` patch was not
applied (``POLICY_UNAVAILABLE``, ``POLICY_HTTP_<status>``, ``NO_POLICY_HOOK``…, null when it was), and ``select``
starts with ``SELECT_FALLBACK`` when the pod is the auction's house ads instead of ``/select``'s answer. Metric filters
count the lines, the policy fallbacks, the select fallbacks and the module's errors (an empty VAST, so the slate
plays), leaving out the warm-up's and the herd's synthetic auctions (``warmup-<n>`` sessions on ``<prefix>-warmup``),
which run on cold tasks by design. Two alarms watch the shares over 5 minutes:

- ``<prefix>-pds-policy-fallbacks``: more than 1% of breaks served without the policy (own book only, no viewer data);
- ``<prefix>-pds-select-fallbacks``: more than 0.5% of pods built by the module from house bids, or empty after an
  error.

Below :data:`MIN_PODS` auctions in 5 minutes a share says little, so there either alarms once :data:`MIN_FALLBACKS`
breaks fell back: a stage with few viewers still alarms when the PDS is down.

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

MIN_PODS: Final = 20
"""Auctions in 5 minutes below which the shares are not judged."""
MIN_FALLBACKS: Final = 5
"""Below :data:`MIN_PODS`, the fallbacks in 5 minutes that alarm all the same."""
PERIOD: Final = Duration.minutes(5)
POLICY_SHARE: Final = 1.0
SELECT_SHARE: Final = 0.5
_POD: Final = '"fast-ctv-pod" -"warmup"'
PATTERNS: Final = {
    "Pods": _POD,
    "PolicyFallbacks": f'{_POD} "POLICY_"',
    "SelectFallbacks": f'{_POD} "SELECT_FALLBACK"',
    "PodErrors": f'{_POD} "error"',
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

        def share(fallbacks: tuple[str, ...], label: str) -> cw.MathExpression:
            counted = " + ".join(f"FILL(f{n}, 0)" for n in range(len(fallbacks)))
            return cw.MathExpression(
                expression=(
                    f"IF(pods >= {MIN_PODS}, 100 * ({counted}) / pods, IF({counted} >= {MIN_FALLBACKS}, 100, 0))"
                ),
                using_metrics={
                    "pods": metrics["Pods"],
                    **{f"f{n}": metrics[name] for n, name in enumerate(fallbacks)},
                },
                label=label,
                period=PERIOD,
            )

        self.policy = alarm(
            self,
            "PolicyFallbacksAlarm",
            name=f"{prefix}-pds-policy-fallbacks",
            metric=share(("PolicyFallbacks",), "Breaks without the /policy patch, %"),
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
            metric=share(("SelectFallbacks", "PodErrors"), "Pods from the module's house fallback or errors, %"),
            threshold=SELECT_SHARE,
            comparison=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            description=f"More than {SELECT_SHARE}% of pods in 5 minutes were the module's house fallback instead of "
            "the PDS's /select answer, or empty after a module error: no paid ad and no reservation in those breaks",
            topic=alert_topic,
        )
