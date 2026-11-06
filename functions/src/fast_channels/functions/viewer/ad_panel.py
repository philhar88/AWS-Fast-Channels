"""The demo's "why these ads" panel API (``modules.viewer.ad_panel``): ``GET /sessions/{sid}/breaks``.

For one MediaTailor session it reads the Pod Decision Service's decision records from the sidecar's log group with
CloudWatch Logs Insights and answers each break's privacy mode, tier, ad-load policy and pod
(:mod:`~fast_channels.functions.viewer.decisions`). Records are queryable seconds after a break is decided.

**Only the viewer's own sessions.** API Gateway's JWT authorizer admits only the viewer pool's access tokens with the
scope ``fast/playback``. The function derives the caller's viewer id from the token's ``iss`` and ``sub`` with the
mint's own function (KMS ``GenerateMac``, ``fast_channels.core.identity.vid``) and answers only when the session's
``pds.policy`` records name that ``vid``. A session without one (anonymous: a guest, or a replayed ticket) is
refused, so nobody reads another viewer's tier. The answer is ``Cache-Control: private, no-store``.

Demo only: it shows a viewer their value tier, which a production viewer app never does (DESIGN §16.4).
"""

import os
import time
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from fast_channels.core.identity.vid import viewer_id
from fast_channels.functions.aws import client
from fast_channels.functions.viewer.decisions import (
    POLICY,
    SESSION_ID,
    insights_query,
    parse_results,
    summarize,
    viewer_ids,
)

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
app = APIGatewayHttpResolver()

LOOKBACK_SECONDS: Final = 6 * 3600
"""How far back a session's records are looked for: longer than any demo session."""
QUERY_DEADLINE_SECONDS: Final = 10.0
"""Longest wait for a query; the function's timeout leaves room to stop it and answer."""
POLL_SECONDS: Final = 0.5
CACHE_SECONDS: Final = 15.0
"""How long one session's records are kept: the panel polls every 20 s, and a refresh within this reuses them."""
NO_STORE: Final = {"Cache-Control": "private, no-store"}

_cached: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _settings() -> tuple[str, str]:
    return os.environ["DECISION_LOG_GROUP"], os.environ["VID_KEY_ID"]


@cache
def caller_vid(issuer: str, sub: str) -> str:
    """The signed-in viewer's ``vid``, computed as the mint computes it (one KMS call per viewer and instance)."""
    return viewer_id(client("kms"), key_id=_settings()[1], issuer=issuer, sub=sub)


class QueryTimeoutError(Exception):
    """Logs Insights did not finish in time."""


def session_records(sid: str, now: float | None = None) -> list[dict[str, Any]]:
    """The decision records of session ``sid`` (a Logs Insights query, kept :data:`CACHE_SECONDS`)."""
    now = time.time() if now is None else now
    if (hit := _cached.get(sid)) and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    logs = client("logs")
    try:
        query = logs.start_query(
            logGroupName=_settings()[0],
            startTime=int(now - LOOKBACK_SECONDS),
            endTime=int(now) + 60,
            queryString=insights_query(sid),
        )["queryId"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        return []  # no sidecar log group yet (before make deploy-demand): nothing was decided
    deadline = time.monotonic() + QUERY_DEADLINE_SECONDS
    while True:
        result = logs.get_query_results(queryId=query)
        match result["status"]:
            case "Complete":
                break
            case "Failed" | "Cancelled" | "Timeout" | "Unknown":
                raise QueryTimeoutError(f"the decision-record query ended {result['status']}")
        if time.monotonic() > deadline:
            logs.stop_query(queryId=query)
            raise QueryTimeoutError("the decision-record query took too long")
        time.sleep(POLL_SECONDS)
    records = parse_results(result.get("results", []))
    metrics.add_metric(name="AdPanelBytesScanned", unit=MetricUnit.Bytes, value=result["statistics"]["bytesScanned"])
    _cached[sid] = (now, records)
    return records


def _answer(status: int, body: dict[str, Any]) -> Response[dict[str, Any]]:
    return Response(status_code=status, content_type=content_types.APPLICATION_JSON, body=body, headers=NO_STORE)


@app.get("/sessions/<sid>/breaks")
def breaks(sid: str) -> Response[dict[str, Any]]:
    if not SESSION_ID.fullmatch(sid):
        return _answer(400, {"message": "Not a MediaTailor session id"})
    claims = app.current_event.request_context.authorizer.jwt_claim or {}
    issuer, sub = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or not isinstance(sub, str):
        return _answer(401, {"message": "No viewer in the token"})
    try:
        records = session_records(sid)
    except QueryTimeoutError as error:
        logger.warning("decision records unavailable", extra={"error": str(error)})
        return _answer(503, {"message": "The decision records are not available right now; try again"})
    if not any(r.get("record") == POLICY for r in records):
        return _answer(200, {"sid": sid, "breaks": []})
    if caller_vid(issuer, sub) not in viewer_ids(records):
        metrics.add_metric(name="AdPanelRefused", unit=MetricUnit.Count, value=1)
        return _answer(403, {"message": "These are not your session's decisions, or the session is anonymous"})
    decisions = summarize(sid, records)
    logger.info("ad panel answered", extra={"breaks": len(decisions.breaks)})
    return _answer(200, decisions.model_dump(mode="json"))


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
