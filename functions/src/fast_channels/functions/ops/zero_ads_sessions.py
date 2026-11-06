"""Find playback sessions that saw at least three ad breaks but made no ad request, and, on the release module's
siblings, breaks that no ad request served (ops module, hourly).

**Zero-ad sessions.** A viewer who forces the session-initialization hook to fail can still pass
``aws.availSuppression*`` and watch without ads; such a session sees the breaks' ad markers (``AD_MARKER_FOUND``) but
makes no ``MAKING_ADS_REQUEST``. This is the detection of that abuse, and it is incomplete by design (a short session,
or one that leaves before its third break, is not found).

**Unserved breaks** (``UNSERVED_BREAKS``). MediaTailor logs a break's start marker (``AD_MARKER_FOUND`` without
``,DURATION=``) each time a session meets it, sometimes several times a break: a sighting it fills from the
retrieval names the ``prefetchScheduleName``, and a later sighting may ask again in real time and replace the pod,
or do nothing (fastcid 2026-11-06). A break is counted once per session, by its ``DATERANGE`` ID: it is served when
any of its sightings is followed by a real-time ``MAKING_ADS_REQUEST`` or a ``FILLED_AVAIL`` of the session within
:data:`SERVED_WITHIN`, or names its prefetch schedule. On prefetching siblings MediaTailor sometimes serves none of
them, with no event at all (W-2 1.33, fastcid 2026-11-06: 3 of 33 sessions, their partners on the same breaks
filled, every marker inside its schedule's consumption window). The silent-loss detector cannot see one: it alarms on
a minute's ratio over at least five markers.

**Refilled breaks** (with ``UNSERVED_BREAKS``). MediaTailor can also meet a prefetched break's start marker again
seconds after filling the break from its retrieval, ask in real time and replace the pod mid-break: the viewer sees
the first ad start twice (W-2 1.34, fastcid 2026-11-06). Delivery counts the repeat once (credit ``repeat``); the
detector counts each break of a session with more than one ``FILLED_AVAIL`` (``RefilledBreaks``), with no alarm.

An EventBridge Scheduler schedule invokes this function every hour with its scheduled time; it runs the queries with
CloudWatch Logs Insights over the hour before, over the MediaTailor vended logs it is given, logs every session and
break it finds, and publishes ``ZeroAdsSessions``, ``UnservedBreaks`` and ``RefilledBreaks`` (the last two with a
``ConfigurationName`` series per sibling). The lake's view ``v_zero_ads_sessions`` answers the first question per day
with Athena.
"""

# OPTIMIZATION(aws): W-2 FR-1 detects ad-free sessions a public endpoint allows: retire with the feature
import re
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
    from collections.abc import Iterable, Sequence

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
    "fields @timestamp, sessionId, originId, eventType, prefetchScheduleName, adBreakCorrelationId\n"
    '| filter (eventType = "AD_MARKER_FOUND" and adMarkers not like /,DURATION=/)'
    ' or eventType in ["MAKING_ADS_REQUEST", "FILLED_AVAIL"]\n'
    '| parse adMarkers /ID="(?<marker>[^"]+)"/\n'
    "| sort @timestamp asc\n"
    f"| limit {ROWS_LIMIT}"
)
"""Every sighting of a break's start marker (with its ``DATERANGE`` ID), ad request and fill: one row each."""
# OPTIMIZATION(aws): W-2 1.33 a prefetched break is neither consumed nor requested: retire when MediaTailor fixes it
SERVED_WITHIN: Final = timedelta(seconds=10)
"""How long after a sighting the session's real-time request or fill must come (seen: 2.8 s)."""
MARKER_EARLY: Final = timedelta(seconds=1)
"""A request or fill logged this much before its sighting still serves it (events of one request, out of order)."""
SIBLING: Final = re.compile(r"-p-[a-z0-9]$")
"""A sibling playback configuration (``<channel>-p-<letter>``): only theirs are counted. A harness's own
configuration in the module's log group (``<prefix>-lab-x``, whose markers make no request on purpose) is not."""
# OPTIMIZATION(aws): W-2 1.34 a prefetched break is filled again in real time: retire when MediaTailor fixes it
FILL_EARLY: Final = timedelta(seconds=30)
"""A fill this long before a break's first logged sighting is the break's own: the sighting a fill from the
retrieval came with is not always logged (seen 7 s); the previous break's fill is at least a program earlier."""
SIGHTINGS_SPAN: Final = timedelta(minutes=3)
"""How far apart a break's sightings by one session can be (seen: 42 s; the longest break is 120 s): each window is
read this much wider on both sides, so a break whose sightings straddle a window's edge is judged whole, in the
window of its first sighting."""
MIN_SLICE: Final = timedelta(minutes=1)
POLL_SECONDS: Final = 2
DEADLINE_SECONDS: Final = 240

logger = Logger()
metrics = Metrics()


class QueryError(RuntimeError):
    """The Logs Insights query failed or did not finish in time."""


@dataclass(frozen=True, slots=True)
class Unserved:
    """A break of a session that neither its retrieval nor an ad request served: its first sighting."""

    session: str
    configuration: str
    marker_at: datetime
    marker: str = ""
    """The break's ``DATERANGE`` ID (else its correlation id)."""
    sightings: int = 1


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


@dataclass(frozen=True, slots=True)
class Refilled:
    """A break of a session MediaTailor filled more than once (W-2 1.34): its first sighting and its fills."""

    session: str
    configuration: str
    marker_at: datetime
    marker: str
    fills: int


@dataclass(frozen=True, slots=True)
class Breaks:
    """What one window's sightings say: the unserved breaks and the refilled ones."""

    unserved: list[Unserved]
    refilled: list[Refilled]

    def __add__(self, other: Breaks) -> Breaks:
        return Breaks(self.unserved + other.unserved, self.refilled + other.refilled)


def judge(rows: Iterable[dict[str, str]], *, start: datetime, end: datetime) -> Breaks:
    """The breaks among :data:`UNSERVED_QUERY`'s ``rows`` whose first sighting is in [``start``, ``end``), counted once
    per session by ``DATERANGE`` ID (only siblings').

    - **Unserved:** none of its sightings names its prefetch schedule or is followed, from :data:`MARKER_EARLY` before
      it to :data:`SERVED_WITHIN` after, by a real-time request or a fill of the session. A prefetch retrieval (a
      request that names its schedule) serves another break.
    - **Refilled:** more than one ``FILLED_AVAIL`` of the session from :data:`FILL_EARLY` before its first sighting
      to :data:`SERVED_WITHIN` after its last.
    """
    sightings: dict[tuple[str, str], list[tuple[datetime, bool, str]]] = defaultdict(list)
    served: dict[str, list[datetime]] = defaultdict(list)
    fills: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        at, session = _at(row), row.get("sessionId", "")
        prefetched = bool(row.get("prefetchScheduleName"))
        match row.get("eventType"):
            case "AD_MARKER_FOUND":
                if not SIBLING.search(row.get("originId", "")):
                    continue
                key = row.get("marker") or row.get("adBreakCorrelationId") or row["@timestamp"]
                sightings[session, key].append((at, prefetched, row.get("originId", "")))
            case "MAKING_ADS_REQUEST" if prefetched:
                continue
            case "FILLED_AVAIL":
                served[session].append(at)
                fills[session].append(at)
            case _:
                served[session].append(at)
    unserved_found, refilled_found = [], []
    for (session, key), seen in sightings.items():
        first, _, configuration = min(seen)
        if not start <= first < end:
            continue
        last = max(at for at, _, _ in seen)
        filled = [t for t in fills[session] if first - FILL_EARLY <= t <= last + SERVED_WITHIN]
        if len(filled) > 1:
            refilled_found.append(Refilled(session, configuration, first, key, len(filled)))
        if any(prefetched for _, prefetched, _ in seen) or any(
            at - MARKER_EARLY <= t <= at + SERVED_WITHIN for at, _, _ in seen for t in served[session]
        ):
            continue
        unserved_found.append(Unserved(session, configuration, first, key, len(seen)))
    return Breaks(sorted(unserved_found, key=_order), sorted(refilled_found, key=_order))


def _order(item: Unserved | Refilled) -> tuple[datetime, str]:
    return item.marker_at, item.session


def unserved(rows: Iterable[dict[str, str]], *, start: datetime, end: datetime) -> list[Unserved]:
    """The unserved breaks among ``rows`` (:func:`judge`)."""
    return judge(rows, start=start, end=end).unserved


def refilled(rows: Iterable[dict[str, str]], *, start: datetime, end: datetime) -> list[Refilled]:
    """The refilled breaks among ``rows`` (:func:`judge`)."""
    return judge(rows, start=start, end=end).refilled


def sighted_breaks(log_group: str, start: datetime, end: datetime) -> Breaks:
    """The unserved and refilled breaks whose first sighting is in [``start``, ``end``). The query reads
    :data:`SIGHTINGS_SPAN` beyond both ends, so every sighting of those breaks and what followed them is seen; a window
    whose read reaches :data:`ROWS_LIMIT` is read again in halves (down to :data:`MIN_SLICE`)."""
    rows = run_query(log_group, start - SIGHTINGS_SPAN, end + SIGHTINGS_SPAN, UNSERVED_QUERY)
    if len(rows) >= ROWS_LIMIT and end - start > MIN_SLICE:
        middle = start + (end - start) / 2
        return sighted_breaks(log_group, start, middle) + sighted_breaks(log_group, middle, end)
    if len(rows) >= ROWS_LIMIT:
        logger.warning("unserved breaks: a minute holds more rows than one query returns", extra={"start": str(start)})
    return judge(rows, start=start, end=end)


def unserved_breaks(log_group: str, start: datetime, end: datetime) -> list[Unserved]:
    """The unserved breaks of :func:`sighted_breaks`."""
    return sighted_breaks(log_group, start, end).unserved


def _publish(name: str, found: Sequence[Unserved | Refilled]) -> None:
    """``name``: the total, and one series per configuration that had any."""
    metrics.add_metric(name=name, unit=MetricUnit.Count, value=len(found))
    by_configuration: dict[str, int] = defaultdict(int)
    for item in found:
        by_configuration[item.configuration] += 1
    for configuration, count in sorted(by_configuration.items()):
        with single_metric(name=name, unit=MetricUnit.Count, value=count, namespace=metrics.namespace) as metric:
            metric.add_dimension(name="service", value=str(metrics.service))
            metric.add_dimension(name="ConfigurationName", value=configuration)


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Count the zero-ad sessions (and, with ``UNSERVED_BREAKS``, the unserved and refilled breaks) of the hour
    before ``scheduled_time`` and log each one."""
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
        breaks = sighted_breaks(config.ads_log_group, end - WINDOW, end)
        for item in breaks.unserved:
            logger.warning(
                "a break's marker got neither its prefetch nor an ad request",
                extra={
                    "session_id": item.session,
                    "configuration": item.configuration,
                    "marker_at": item.marker_at.isoformat(),
                    "marker": item.marker,
                    "sightings": item.sightings,
                },
            )
        for refill in breaks.refilled:
            logger.info(
                "a prefetched break was filled again in real time",
                extra={
                    "session_id": refill.session,
                    "configuration": refill.configuration,
                    "marker_at": refill.marker_at.isoformat(),
                    "marker": refill.marker,
                    "fills": refill.fills,
                },
            )
        _publish("UnservedBreaks", breaks.unserved)
        _publish("RefilledBreaks", breaks.refilled)
        counts["unserved_breaks"] = len(breaks.unserved)
        counts["refilled_breaks"] = len(breaks.refilled)
    return counts
