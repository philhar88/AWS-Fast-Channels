"""``POST /ownbook/openrtb``: the own book, the OpenRTB bidder that Prebid Server knows as ``ownbook``.

For each imp it bids the house ads and the creatives of each line with budget left that fit the slot
(``fast_channels.core.pds.book``), from the current snapshot and the lines' budgets (``state.budgets``), both read off
the request path. No bid is an HTTP 204, which Prebid Server reads as no bid; so is a sidecar without a snapshot (an
empty book). A request it cannot read is a 400, which Prebid Server logs as the bidder's error; the auction goes on
without the own book, and the module's house fallback has nothing to render.
"""

import json

from aws_lambda_powertools import Logger
from pydantic import ValidationError

from fast_channels.core.pds.book import BidRequest, response
from fast_channels.pds import state
from fast_channels.pds.http import INTERNAL, NO_BID, REFUSED, Answer, refused_fields
from fast_channels.pds.telemetry import counters

logger = Logger(child=True)

store = state.store
"""The sidecar's snapshot store."""
budgets = state.budgets
"""The lines' budgets."""


def answer(body: bytes, started: float) -> Answer:
    """The bids, a 204 for none, a 400 for a request that is not OpenRTB, or a 500."""
    try:
        request = BidRequest.model_validate_json(body)
    except ValidationError as error:
        logger.warning("own-book request refused", extra={"fields": refused_fields(error)})
        counters.add("OwnBookRefused")
        return REFUSED
    try:
        return _answer(request)
    except Exception:
        logger.exception("own book failed")
        counters.add("OwnBookErrors")
        return INTERNAL


def _answer(request: BidRequest) -> Answer:
    snapshot = store().get()
    exhausted = budgets().exhausted(snapshot.book)
    answered = response(request, snapshot.book, exhausted=exhausted)
    bids = answered["seatbid"][0]["bid"] if answered is not None else []
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
    counters.add("OwnBookBids", len(bids))
    if answered is None:
        return NO_BID
    return Answer(200, json.dumps(answered))
