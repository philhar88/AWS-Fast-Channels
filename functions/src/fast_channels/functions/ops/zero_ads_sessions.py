"""Find playback sessions that saw at least three ad breaks but made no ad request (ops module, hourly).

A viewer who forces the session-initialization hook to fail can still pass ``aws.availSuppression*`` and watch
without ads; such a session sees the breaks' ad markers (``AD_MARKER_FOUND``) but makes no ``MAKING_ADS_REQUEST``.
This is the detection of that abuse, and it is incomplete by design (a short session, or one that leaves before its
third break, is not found). An EventBridge Scheduler schedule invokes this function every hour with its scheduled
time; it runs :data:`QUERY` with CloudWatch Logs Insights over the hour before, over the deployment's MediaTailor
vended logs, logs every session it finds and publishes their number as ``ZeroAdsSessions``. The lake's view
``v_zero_ads_sessions`` answers the same question per day with Athena.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from fast_channels.functions import aws
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
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
POLL_SECONDS: Final = 2
DEADLINE_SECONDS: Final = 240

logger = Logger()
metrics = Metrics()


class QueryError(RuntimeError):
    """The Logs Insights query failed or did not finish in time."""


def run_query(log_group: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    """Rows of :data:`QUERY` over ``log_group`` between ``start`` and ``end``, as field -> value.

    Raises:
        QueryError: the query failed, was cancelled or did not complete within :data:`DEADLINE_SECONDS`.
    """
    client = aws.client("logs")
    query_id = client.start_query(
        logGroupNames=[log_group],
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=QUERY,
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


@metrics.log_metrics
@logger.inject_lambda_context
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    """Count the zero-ad sessions of the hour before ``scheduled_time`` and log each one."""
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
    return {"sessions": len(sessions)}
