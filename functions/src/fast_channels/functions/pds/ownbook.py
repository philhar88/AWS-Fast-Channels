"""``POST /ownbook/openrtb``: the own book, the OpenRTB bidder that Prebid Server knows as ``ownbook``.

Behind the PDS's internal load balancer (Powertools ``ALBResolver``). For each imp it bids the house ads and the
creatives of each line with budget left that fit the slot (``fast_channels.core.pds.book``), from the current snapshot
and the lines' budgets (``budget``), both read off the request path. No bid is an HTTP 204, which Prebid Server reads
as no bid. A request it cannot read is a 400, which Prebid Server logs as the bidder's error; the auction goes on
without the own book, and the module's house fallback has nothing to render.
"""

import json
import os
from functools import cache
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.event_handler import ALBResolver, Response, content_types
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import ValidationError

from fast_channels.core.pds.book import BidRequest, response
from fast_channels.core.pds.names import Endpoint
from fast_channels.functions.pds.budget import Budgets
from fast_channels.functions.pds.store import snapshot_store
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
app = ALBResolver()


store = snapshot_store
"""The function's snapshot store (one per execution environment)."""


@cache
def budgets() -> Budgets:
    """The lines' budgets, for the lines of the snapshot the function has."""
    settings().require("pds_table")
    return Budgets(settings().pds_table, lines=lambda: store().current.lines)


@app.post(Endpoint.OWN_BOOK.value)
def own_book() -> Response[str]:
    """The bids, a 204 for none, a 400 for a request that is not OpenRTB, or a 500."""
    try:
        request = BidRequest.model_validate_json(app.current_event.decoded_body or "")
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in e["loc"]) for e in error.errors()})
        logger.warning("own-book request refused", extra={"fields": fields})
        metrics.add_metric(name="OwnBookRefused", unit=MetricUnit.Count, value=1)
        return Response(status_code=400, content_type=content_types.APPLICATION_JSON, body='{"error":"invalid"}')
    try:
        return _answer(request)
    except Exception:
        logger.exception("own book failed")
        metrics.add_metric(name="OwnBookErrors", unit=MetricUnit.Count, value=1)
        return Response(status_code=500, content_type=content_types.APPLICATION_JSON, body='{"error":"internal"}')


def _answer(request: BidRequest) -> Response[str]:
    snapshot = store().get()
    exhausted = budgets().exhausted(snapshot.book)
    answer = response(request, snapshot.book, exhausted=exhausted)
    bids = answer["seatbid"][0]["bid"] if answer is not None else []
    logger.info(
        "own book",
        extra={
            "request_id": request.id,
            "imps": len(request.imp),
            "durations": sorted({d for imp in request.imp for d in imp.durations}),
            "house": sum(1 for bid in bids if bid["ext"]["fast"].get("house")),
            "lines": sorted({bid["ext"]["fast"]["line"] for bid in bids if "line" in bid["ext"]["fast"]}),
            "exhausted": sorted(exhausted),
            "bids": len(bids),
            "snapshot": snapshot.version,
        },
    )
    metrics.add_metric(name="OwnBookBids", unit=MetricUnit.Count, value=len(bids))
    if answer is None:
        return Response(status_code=204, content_type=content_types.APPLICATION_JSON, body="")
    return Response(status_code=200, content_type=content_types.APPLICATION_JSON, body=json.dumps(answer))


@logger.inject_lambda_context(clear_state=True)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):  # pragma: no cover - read both during init, not on a request
    store().load()
    budgets().load()
