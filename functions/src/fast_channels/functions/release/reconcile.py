"""The siblings' ADS log against MediaTailor's own metrics (hourly): ad requests MediaTailor made but did not log.

MediaTailor can leave a whole break out of its vended ADS log while it requested and filled it (fastm, 2026-10-29:
no first ``AD_MARKER_FOUND``, ``MAKING_ADS_REQUEST`` or ``FILLED_AVAIL``, while ``AWS/MediaTailor`` counted the
avail and the Pod Decision Service received the request). The silent-loss detector counts logged markers, so it is
blind to such a gap; and the Pod Decision Service counts deliveries from the same log's ``BEACON_FIRED``, so a gap
under-counts delivery.

Every hour, for each sibling, over an hour that ended :data:`LAG` ago (the vended logs' delivery delay):

- **requests:** ``SampleCount`` of ``AWS/MediaTailor`` ``AdDecisionServer.Ads`` (one sample per ADS response)
  against the log's ``MAKING_ADS_REQUEST`` events. On fastm the two agreed hour by hour, except the hour of the gap;
- **fills** (reported, not alarmed): ``SampleCount`` of ``Avail.FilledDuration`` (one sample per avail) against the
  log's ``FILLED_AVAIL`` events. An unfilled avail has no ``FILLED_AVAIL``, so this differs without any gap.

``UnloggedAdsRequests`` is the requests' shortfall, summed over the siblings; ``<prefix>-release-log-loss`` alarms on
any. The log window is a minute wider than the metrics' on each side, so an event logged across a minute boundary
is not a false shortfall. This detects; it never back-fills delivery: reconcile delivery from the trackers' own logs
(runbook in docs/release-controller.md).
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.core.monetization import names
from fast_channels.functions import aws
from fast_channels.functions.release import smoke
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
WINDOW: Final = timedelta(hours=1)
LAG: Final = timedelta(minutes=20)
"""The vended logs reach the log group within minutes; an hour that ended this long ago is complete."""
EDGE: Final = timedelta(minutes=1)
QUERY: Final = (
    "filter eventType in ['MAKING_ADS_REQUEST', 'FILLED_AVAIL']\n| stats count(*) as n by originId, eventType"
)
METRICS: Final = {"requests": "AdDecisionServer.Ads", "avails": "Avail.FilledDuration"}


def window(at: datetime) -> tuple[datetime, datetime]:
    """The hour, on minute boundaries, that ended :data:`LAG` before ``at``."""
    end = (at - LAG).replace(second=0, microsecond=0)
    return end - WINDOW, end


def counted(siblings: Sequence[str], start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """``AWS/MediaTailor``'s sample counts per sibling: ADS responses and avails."""
    queries = [
        {
            "Id": f"m{n}_{kind}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/MediaTailor",
                    "MetricName": metric,
                    "Dimensions": [{"Name": "ConfigurationName", "Value": sibling}],
                },
                "Period": int(WINDOW.total_seconds()),
                "Stat": "SampleCount",
            },
        }
        for n, sibling in enumerate(siblings)
        for kind, metric in METRICS.items()
    ]
    found: dict[str, dict[str, int]] = {s: dict.fromkeys(METRICS, 0) for s in siblings}
    client = aws.client("cloudwatch")
    for chunk in range(0, len(queries), 500):
        result = client.get_metric_data(MetricDataQueries=queries[chunk : chunk + 500], StartTime=start, EndTime=end)
        for series in result["MetricDataResults"]:
            n, kind = series["Id"][1:].split("_", 1)
            found[siblings[int(n)]][kind] = int(sum(series.get("Values", [])))
    return found


def logged(start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """The siblings' ADS log events per sibling: ``MAKING_ADS_REQUEST`` and ``FILLED_AVAIL``."""
    config = settings()
    config.require("prefix")
    rows = smoke.insights(names.log_group(config.prefix), QUERY, (start - EDGE).timestamp(), (end + EDGE).timestamp())
    found: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = "requests" if row["eventType"] == "MAKING_ADS_REQUEST" else "avails"
        found.setdefault(row["originId"], dict.fromkeys(METRICS, 0))[kind] = int(row["n"])
    return found


def reconcile(siblings: Sequence[str], at: datetime) -> dict[str, Any]:
    """Each sibling's counted and logged requests and avails over the hour :func:`window` gives, and the requests'
    shortfall."""
    start, end = window(at)
    by_metrics, by_log = counted(siblings, start, end), logged(start, end)
    missing = {
        sibling: max(0, by_metrics[sibling]["requests"] - by_log.get(sibling, {}).get("requests", 0))
        for sibling in siblings
    }
    report = {
        sibling: {
            "counted": by_metrics[sibling],
            "logged": by_log.get(sibling, dict.fromkeys(METRICS, 0)),
            "unlogged_requests": missing[sibling],
        }
        for sibling in siblings
    }
    unlogged = sum(missing.values())
    short = {s: report[s] for s, n in missing.items() if n}
    if short:
        logger.warning("ad requests missing from the ADS log", extra={"start": start.isoformat(), "siblings": short})
    return {"start": start.isoformat(), "end": end.isoformat(), "unlogged_requests": unlogged, "siblings": report}


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The scheduled run (``scheduled_time``, or now)."""
    config = settings()
    config.require("release_siblings")
    scheduled = event.get("scheduled_time")
    at = datetime.fromisoformat(scheduled.replace("Z", "+00:00")) if scheduled else datetime.now(UTC)
    result = reconcile(config.release_siblings, at)
    metrics.add_metric(name="UnloggedAdsRequests", unit=MetricUnit.Count, value=result["unlogged_requests"])
    return result
