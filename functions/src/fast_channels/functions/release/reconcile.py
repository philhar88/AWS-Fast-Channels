"""The siblings' ADS log against MediaTailor's own metrics (hourly): filled ad time MediaTailor did not log.

MediaTailor can leave a whole break out of its vended ADS log while it requested and filled it (fastm, 2026-10-29:
no first ``AD_MARKER_FOUND``, ``MAKING_ADS_REQUEST`` or ``FILLED_AVAIL``, while ``AWS/MediaTailor`` counted the
avail and the Pod Decision Service received the request). The silent-loss detector counts logged markers, so it is
blind to such a gap; and the Pod Decision Service counts deliveries from the same log's ``BEACON_FIRED``, so a gap
under-counts delivery.

Every hour, for each sibling, over an hour that ended :data:`LAG` ago (the vended logs' delivery delay), the filled
ad time ``AWS/MediaTailor`` counted (the ``Sum`` of ``Avail.FilledDuration``, in milliseconds) is compared with the
log's (the sum of ``FILLED_AVAIL``'s ``avail.filledDuration``, in seconds). Both are emitted once per avail, at the
break, whether its ads came from a prefetch retrieval or from a real-time request, and an unfilled avail adds 0 to
both: on fastm (the Prebid Server route, prefetch on) and fastcih (the ad-server route) they agreed to the millisecond
(2026-10-30). Request counts do not reconcile: ``AdDecisionServer.Ads`` has one sample per avail at the break, while
the log has a ``MAKING_ADS_REQUEST`` per retrieval and per VAST Wrapper hop.

``UnloggedFilledSeconds`` is the shortfall, summed over the siblings (beyond :data:`TOLERANCE_MS` per sibling, for
rounding); ``<prefix>-release-log-loss`` alarms on any. The log window is a minute wider than the metrics' on each
side, so an avail logged across a minute boundary is not a false shortfall. Avail counts are reported for context
(an unfilled avail has a sample and no ``FILLED_AVAIL``). This detects; it never back-fills delivery: reconcile
delivery from the trackers' own logs (runbook in docs/release-controller.md).
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
TOLERANCE_MS: Final = 1000
"""The log's durations are seconds with two decimals; a dropped break is at least 30 s."""
QUERY: Final = (
    "filter eventType = 'FILLED_AVAIL'\n| stats count(*) as avails, sum(avail.filledDuration) as filled_s by originId"
)
FILLED: Final = "Avail.FilledDuration"


def window(at: datetime) -> tuple[datetime, datetime]:
    """The hour, on minute boundaries, that ended :data:`LAG` before ``at``."""
    end = (at - LAG).replace(second=0, microsecond=0)
    return end - WINDOW, end


def counted(siblings: Sequence[str], start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """``AWS/MediaTailor``'s avails and filled milliseconds per sibling (``Avail.FilledDuration``)."""
    queries = [
        {
            "Id": f"m{n}_{kind}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/MediaTailor",
                    "MetricName": FILLED,
                    "Dimensions": [{"Name": "ConfigurationName", "Value": sibling}],
                },
                "Period": int(WINDOW.total_seconds()),
                "Stat": stat,
            },
        }
        for n, sibling in enumerate(siblings)
        for kind, stat in (("avails", "SampleCount"), ("filled_ms", "Sum"))
    ]
    found: dict[str, dict[str, int]] = {s: {"avails": 0, "filled_ms": 0} for s in siblings}
    client = aws.client("cloudwatch")
    for chunk in range(0, len(queries), 500):
        result = client.get_metric_data(MetricDataQueries=queries[chunk : chunk + 500], StartTime=start, EndTime=end)
        for series in result["MetricDataResults"]:
            n, kind = series["Id"][1:].split("_", 1)
            found[siblings[int(n)]][kind] = round(sum(series.get("Values", [])))
    return found


def logged(start: datetime, end: datetime) -> dict[str, dict[str, int]]:
    """The siblings' logged avails and filled milliseconds per sibling (``FILLED_AVAIL``)."""
    config = settings()
    config.require("prefix")
    rows = smoke.insights(names.log_group(config.prefix), QUERY, (start - EDGE).timestamp(), (end + EDGE).timestamp())
    return {
        row["originId"]: {"avails": int(row["avails"]), "filled_ms": round(float(row.get("filled_s") or 0) * 1000)}
        for row in rows
    }


def created(group: str) -> datetime | None:
    """When the log group ``group`` was created (None if it does not exist)."""
    pages = aws.client("logs").get_paginator("describe_log_groups").paginate(logGroupNamePrefix=group)
    found = next((g for page in pages for g in page["logGroups"] if g["logGroupName"] == group), None)
    return datetime.fromtimestamp(found["creationTime"] / 1000, UTC) if found else None


def reconcile(siblings: Sequence[str], at: datetime) -> dict[str, Any]:
    """Each sibling's counted and logged avails and filled time over the hour :func:`window` gives, and the filled
    time the log is missing.

    An hour that began before the siblings' log group existed is skipped (``skipped``): Logs Insights refuses a query
    that ends before a group's creation, and avails served before the siblings' deliveries existed were never logged.
    The first hour reconciled on a new stage starts after the group's creation.
    """
    start, end = window(at)
    config = settings()
    config.require("prefix")
    born = created(names.log_group(config.prefix))
    if born is None or born > start:
        reason = "no siblings' log group yet" if born is None else f"the siblings' log group was created {born}"
        logger.info("hour skipped", extra={"start": start.isoformat(), "reason": reason})
        return {"start": start.isoformat(), "end": end.isoformat(), "unlogged_filled_s": 0.0, "skipped": reason}
    by_metrics, by_log = counted(siblings, start, end), logged(start, end)
    nothing = {"avails": 0, "filled_ms": 0}
    missing: dict[str, float] = {}
    for sibling in siblings:
        short_ms = by_metrics[sibling]["filled_ms"] - by_log.get(sibling, nothing)["filled_ms"]
        missing[sibling] = short_ms / 1000 if short_ms > TOLERANCE_MS else 0.0
    report = {
        sibling: {
            "counted": by_metrics[sibling],
            "logged": by_log.get(sibling, nothing),
            "unlogged_filled_s": missing[sibling],
        }
        for sibling in siblings
    }
    short = {s: report[s] for s, seconds in missing.items() if seconds}
    if short:
        logger.warning("filled ad time missing from the ADS log", extra={"start": start.isoformat(), "siblings": short})
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "unlogged_filled_s": round(sum(missing.values()), 3),
        "siblings": report,
    }


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """The scheduled run (``scheduled_time``, or now)."""
    config = settings()
    config.require("release_siblings")
    scheduled = event.get("scheduled_time")
    at = datetime.fromisoformat(scheduled.replace("Z", "+00:00")) if scheduled else datetime.now(UTC)
    result = reconcile(config.release_siblings, at)
    if "skipped" not in result:  # an hour not measured has no datapoint (the alarm treats it as not breaching)
        metrics.add_metric(name="UnloggedFilledSeconds", unit=MetricUnit.Seconds, value=result["unlogged_filled_s"])
    return result
