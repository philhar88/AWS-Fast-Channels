"""Find playback sessions that saw at least three ad breaks but made no ad request, and, on the release module's
siblings, breaks that no ad request served (ops module, hourly).

**Zero-ad sessions.** A viewer who forces the session-initialization hook to fail can still pass
``aws.availSuppression*`` and watch without ads; such a session sees the breaks' ad markers (``AD_MARKER_FOUND``) but
makes no ``MAKING_ADS_REQUEST``. This is the detection of that abuse, and it is incomplete by design (a short session,
or one that leaves before its third break, is not found).

**Unserved breaks** (``UNSERVED_BREAKS``). A break's first marker (not the one that gained ``,DURATION=`` at the
break's end) without a ``prefetchScheduleName`` means MediaTailor did not fill the break from a retrieval; it should
then ask in real time. On prefetching siblings it sometimes does neither, with no event at all (fastcid 2026-11-06:
3 of 33 sessions, their partners on the same breaks filled, every marker inside its schedule's consumption window).
Each such marker with no ``MAKING_ADS_REQUEST`` and no ``FILLED_AVAIL`` of the same session within
:data:`SERVED_WITHIN` is an unserved break. The silent-loss detector cannot see one: it alarms on a minute's ratio over
at least five markers.

An EventBridge Scheduler schedule invokes this function every hour with its scheduled time; it runs the queries with
CloudWatch Logs Insights over the hour before, over the MediaTailor vended logs it is given, logs every session and
break it finds, and publishes ``ZeroAdsSessions`` and ``UnservedBreaks`` (with a ``ConfigurationName`` series per
sibling). The lake's view ``v_zero_ads_sessions`` answers the first question per day with Athena.
"""

# OPTIMIZATION(aws): W-2 FR-1 detects ad-free sessions a public endpoint allows: retire with the feature
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aws_lambda_powertools.utilities.typing import LambdaContext

MIN_BREAKS: Final = 3
WINDOW: Final = timedelta(hours=1)
QUERY: Final = (
    'filter eventType in ["AD_MARKER_FOUND", "MAKING_ADS_REQUEST"]\n'
    '| parse adMarkers /ID="(?<marker>[^"]+)"/\n'
    '| stats count_distinct(marker) as breaks, sum(strcontains(eventType, "MAKING_ADS_REQUEST")) as requests '
    "by sessionId, originId\n"
    f"| filter breaks >= {MIN_BREAKS} and requests = 0\n"
    "| sort breaks desc\n"
    "| limit 1000"
)
"""Sessions with at least :data:`MIN_BREAKS` distinct ad markers (``DATERANGE`` IDs) and no ad request."""
ROWS_LIMIT: Final = 10_000
"""Logs Insights returns at most this many rows: a window that reaches it is read again in halves."""
UNSERVED_QUERY: Final = (
    "fields @timestamp, sessionId, originId, eventType\n"
    '| filter (eventType = "AD_MARKER_FOUND" and adMarkers not like /,DURATION=/'
    " and not ispresent(prefetchScheduleName))"
    ' or eventType in ["MAKING_ADS_REQUEST", "FILLED_AVAIL"]\n'
    "| sort @timestamp asc\n"
    f"| limit {ROWS_LIMIT}"
)
"""Breaks' first markers that no retrieval filled, and every ad request and fill (one row each)."""
# OPTIMIZATION(aws): W-2 1.33 a prefetched break is neither consumed nor requested: retire when MediaTailor fixes it
SERVED_WITHIN: Final = timedelta(seconds=10)
"""How long after a first marker the session's real-time request or fill must come (seen: 2.8 s)."""
MARKER_EARLY: Final = timedelta(seconds=1)
"""A request or fill logged this much before its marker still serves it (events of one request, logged out of order)."""
MIN_SLICE: Final = timedelta(minutes=1)
POLL_SECONDS: Final = 2
DEADLINE_SECONDS: Final = 240
MARKER: Final = "AD_MARKER_FOUND"

logger = Logger()
metrics = Metrics()


class QueryError(RuntimeError):
    """The Logs Insights query failed or did not finish in time."""


@dataclass(frozen=True, slots=True)
class Unserved:
    """A break's first marker that neither a retrieval nor an ad request served."""

    session: str
    configuration: str
    marker_at: datetime


def run_query(log_group: str, start: datetime, end: datetime, query: str = QUERY) -> list[dict[str, str]]:
    """Rows of ``query`` over ``log_group`` between ``start`` and ``end``, as field -> value.

    Raises:
        QueryError: the query failed, was cancelled or did not complete within :data:`DEADLINE_SECONDS`.
    """
    client = aws.client("logs")
    query_id = client.start_query(
        logGroupNames=[log_group],
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=query,
    )["queryId"]
    deadline = time.monotonic() + DEADLINE_SECONDS
    while True:
        result: dict[str, Any] = client.get_query_results(queryId=query_id)
        match result["status"]:
            case "Complete":
                return [{cell["field"]: cell["value"] for cell in row} for row in result.get("results", [])]
            case "Failed" | "Cancelled" | "Timeout" | "Unknown":
                raise QueryError(f"Logs Insights query {query_id} ended {result['status']}")
        if time.monotonic() > deadline:
            raise QueryError(f"Logs Insights query {query_id} did not complete in {DEADLINE_SECONDS} s")
        time.sleep(POLL_SECONDS)


def _at(row: dict[str, str]) -> datetime:
    """A row's ``@timestamp`` (Logs Insights: ``2026-11-06 09:35:13.938``, UTC)."""
    return datetime.strptime(row["@timestamp"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)


def unserved(rows: Iterable[dict[str, str]], *, start: datetime, end: datetime) -> list[Unserved]:
    """The unserved breaks among :data:`UNSERVED_QUERY`'s ``rows`` whose marker is in [``start``, ``end``): a first
    marker with no request or fill of its session from :data:`MARKER_EARLY` before it to :data:`SERVED_WITHIN`
    after."""
    markers: list[tuple[str, str, datetime]] = []
    served: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        at, session = _at(row), row.get("sessionId", "")
        if row.get("eventType") == MARKER:
            if start <= at < end:
                markers.append((session, row.get("originId", ""), at))
        else:
            served[session].append(at)
    return [
        Unserved(session, configuration, at)
        for session, configuration, at in markers
        if not any(at - MARKER_EARLY <= t <= at + SERVED_WITHIN for t in served[session])
    ]


def unserved_breaks(log_group: str, start: datetime, end: datetime) -> list[Unserved]:
    """The unserved breaks whose marker is in [``start``, ``end``). The query reads :data:`SERVED_WITHIN` past
    ``end`` for the requests and fills of the window's last markers; a window that reaches :data:`ROWS_LIMIT` is read
    again in halves (down to :data:`MIN_SLICE`)."""
    rows = run_query(log_group, start, end + SERVED_WITHIN, UNSERVED_QUERY)
    if len(rows) >= ROWS_LIMIT and end - start > MIN_SLICE:
        middle = start + (end - start) / 2
        return unserved_breaks(log_group, start, middle) + unserved_breaks(log_group, middle, end)
    if len(rows) >= ROWS_LIMIT:
        logger.warning("unserved breaks: a minute holds more rows than one query returns", extra={"start": str(start)})
    return unserved(rows, start=start, end=end)


def _publish_unserved(found: list[Unserved]) -> None:
    """``UnservedBreaks``: the total, and one series per configuration that had any."""
    metrics.add_metric(name="UnservedBreaks", unit=MetricUnit.Count, value=len(found))
    by_configuration: dict[str, int] = defaultdict(int)
    for item in found:
        by_configuration[item.configuration] += 1
    for configuration, count in sorted(by_configuration.items()):
        with single_metric(
            name="UnservedBreaks", unit=MetricUnit.Count, value=count, namespace=metrics.namespace
        ) as metric:
            metric.add_dimension(name="service", value=str(metrics.service))
            metric.add_dimension(name="ConfigurationName", value=configuration)


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Count the zero-ad sessions (and, with ``UNSERVED_BREAKS``, the unserved breaks) of the hour before
    ``scheduled_time`` and log each one."""
    config = settings()
    config.require("ads_log_group")
    end = datetime.fromisoformat(event["scheduled_time"]).astimezone(UTC)
    logger.set_correlation_id(f"zero-ads-{end.isoformat().replace('+00:00', 'Z')}")
    sessions = run_query(config.ads_log_group, end - WINDOW, end)
    for session in sessions:
        logger.warning(
            "a session saw ad breaks but requested no ads",
            extra={
                "session_id": session.get("sessionId"),
                "configuration": session.get("originId"),
                "breaks": session.get("breaks"),
            },
        )
    metrics.add_metric(name="ZeroAdsSessions", unit=MetricUnit.Count, value=len(sessions))
    counts = {"sessions": len(sessions)}
    if config.unserved_breaks:
        found = unserved_breaks(config.ads_log_group, end - WINDOW, end)
        for item in found:
            logger.warning(
                "a break's marker got neither its prefetch nor an ad request",
                extra={
                    "session_id": item.session,
                    "configuration": item.configuration,
                    "marker_at": item.marker_at.isoformat(),
                },
            )
        _publish_unserved(found)
        counts["unserved_breaks"] = len(found)
    return counts
