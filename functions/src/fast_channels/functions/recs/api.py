"""``GET /rail[?profile=kids&device=<d>&lang=<l>]``: the signed-in viewer's "Recommended for you" rail.

No model runs here (DESIGN §15). The router (:func:`~fast_channels.core.recs.router.route`) picks the rail, and the
answer says which it is and why (``strategy``, ``reason``):

- **The viewer's model rail** (``llm``), built earlier by the rail builder, placed on the guide now. A rail that is
  missing, or older than ``refresh_hours``, is rebuilt asynchronously (one build per viewer at a time); meanwhile a
  rail that still airs is served as it is, and otherwise the request gets the rule-based rail (``building``). A
  failed build is served as the rule-based rail with its reason (``cap``, ``timeout``, ``model_error``,
  ``invalid``) for an hour before the next try.
- **The cohort rail** (``cohort``) for opted-out viewers, viewers without a current consent record, viewers without
  taste facts and an account's first session (re-ranked by the declared genres), read from the media bucket.
- **The rule-based rail** (``baseline``) for the kill switch and the holdout.
- **The kids lineup** (``kids``) for a kids profile.

API Gateway's JWT authorizer admits only the viewer pool's access tokens with the scope ``fast/playback``. The
viewer's id is computed from the token's ``iss`` and ``sub`` as the mint computes it; the taste facts are the demo
persona's declared genres (the ``fast:persona`` claim). Answers are ``Cache-Control: private, no-store``.
"""

import json
import time
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response, content_types
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.core.identity import consent
from fast_channels.core.identity.names import PERSONA_CLAIM
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.pds.policy import Consent
from fast_channels.core.personas import PERSONAS
from fast_channels.core.recs.candidates import Cohort, Taste
from fast_channels.core.recs.rail import Airing, Daypart
from fast_channels.core.recs.router import Personalization, Reason, Strategy, route
from fast_channels.core.recs.serve import Answer, RailDoc, answer, baseline, cohort_key, kids_lineup, place, seeded
from fast_channels.core.recs.validate import MIN_TILES
from fast_channels.functions.aws import client, table
from fast_channels.functions.recs import inputs, store
from fast_channels.functions.recs.settings import recs_settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()
app = APIGatewayHttpResolver()

NO_STORE: Final = {"Cache-Control": "private, no-store"}
RETRY_AFTER_FAILURE_SECONDS: Final = 3600
"""A failed build is not retried for an hour: a capped or failing model is not hammered by every request."""
_TASTES: Final = {p.id: Taste(genres=p.genres) for p in PERSONAS if p.genres}


@cache
def caller_vid(issuer: str, sub: str) -> str:
    """The signed-in viewer's ``vid``, as the mint computes it (one KMS call per viewer and instance)."""
    return viewer_id(client("kms"), key_id=recs_settings().vid_key, issuer=issuer, sub=sub)


def personalization(vid: str, now: datetime) -> Personalization:
    """What the viewer's consent row allows; an unreadable store allows nothing (SAFE)."""
    try:
        item = table(recs_settings().identity_table).get_item(Key={"pk": consent.key(vid)}).get("Item")
        row = consent.ConsentRecord.model_validate(item) if item else None
    except ClientError, BotoCoreError, ValidationError:
        logger.warning("consent unreadable: no personalization", exc_info=True)
        return Personalization.UNAVAILABLE
    match consent.consent_of(row, now=now):
        case Consent.FULL | Consent.PSEUDONYMOUS:
            return Personalization.ALLOWED
        case Consent.OPTED_OUT:
            return Personalization.OPTED_OUT
        case _:
            return Personalization.UNAVAILABLE


def build_later(vid: str, taste: Taste) -> None:
    """Ask the rail builder for the viewer's model rail, unless a build is already running."""
    config = recs_settings()
    if not store.claim_build(table(config.table), vid):
        return
    client("lambda").invoke(
        FunctionName=config.builder_function,
        InvocationType="Event",
        Payload=json.dumps({"vid": vid, "taste": taste.model_dump(mode="json")}).encode(),
    )


def model_rail(vid: str, taste: Taste, guides: list[list[Airing]], now: datetime) -> Answer:
    """The viewer's model rail if one still airs, else the rule-based rail with the reason; rebuilds as needed."""
    config = recs_settings()
    recs = table(config.table)
    state = store.viewer(recs, vid)
    clock = now.timestamp()
    doc = store.get_rail(recs, state.rail_key, now=clock) if state.rail_key else None
    placed = place(doc.tiles, guides, now) if doc else []
    stale = state.built_at is None or clock - state.built_at > config.refresh_hours * 3600
    failed_recently = (
        state.failure is not None
        and state.failed_at is not None
        and clock - state.failed_at < RETRY_AFTER_FAILURE_SECONDS
    )
    if (stale or len(placed) < MIN_TILES) and not failed_recently:
        build_later(vid, taste)
    if doc is not None and len(placed) >= MIN_TILES:
        return answer(Strategy.LLM, Reason.MODEL, placed, title=doc.title, lineage=doc.lineage)
    if failed_recently and state.failure in {r.value for r in Reason}:
        reason = Reason(str(state.failure))
    else:
        reason = Reason.EXPIRED if doc is not None else Reason.BUILDING
    return answer(Strategy.BASELINE, reason, baseline(taste.genres, guides, now))


def cohort_rail(
    reason: Reason, taste: Taste, guides: list[list[Airing]], now: datetime, *, device: str, language: str
) -> Answer:
    """The cohort rail of the request's device and language (a configured cohort, else the first one), re-ranked by
    the declared genres in an account's first session; the rule-based rail while it does not exist."""
    config = recs_settings()
    spec = f"{device}-{language}" if f"{device}-{language}" in config.cohorts else config.cohorts[0]
    cohort_device, _, cohort_language = spec.partition("-")
    cohort = Cohort(device=cohort_device, language=cohort_language, daypart=Daypart.at(now))
    try:
        body = client("s3").get_object(Bucket=config.media_bucket, Key=cohort_key(cohort))["Body"].read()
        doc = RailDoc.model_validate_json(body)
    except ClientError, ValidationError:
        doc = None
    placed = place(doc.tiles, guides, now) if doc else []
    if doc is None or len(placed) < MIN_TILES:
        return answer(Strategy.BASELINE, Reason.UNAVAILABLE, baseline(taste.genres, guides, now))
    if reason is Reason.FIRST_SESSION and taste.genres:
        genres_of = {a.content_id: a.genres for g in guides for a in g if a.content_id}
        placed = seeded(placed, taste.genres, genres_of)
    return answer(Strategy.COHORT, reason, placed, title=doc.title, lineage=doc.lineage)


def rail_for(claims: dict[str, Any], query: dict[str, str], now: datetime) -> Answer:
    """The rail of one request (module docstring)."""
    config = recs_settings()
    guides = inputs.guides()
    if query.get("profile") == "kids":
        return answer(Strategy.KIDS, Reason.KIDS, kids_lineup(guides, now, child_directed=config.child_directed))
    vid = caller_vid(str(claims["iss"]), str(claims["sub"]))
    taste = _TASTES.get(str(claims.get(PERSONA_CLAIM, "")), Taste())
    auth_time = int(claims.get("auth_time") or 0)
    recs = table(config.table)
    state = store.viewer(recs, vid)
    if state.first_auth is None:
        state = store.seen(recs, vid, auth_time)
    strategy, reason = route(
        vid=vid,
        kill_switch=inputs.kill_switch(),
        kids=False,
        personalization=personalization(vid, now),
        first_session=state.first_auth == auth_time,
        holdout_percent=config.holdout_percent,
    )
    if strategy is Strategy.LLM and not taste.genres:
        strategy, reason = Strategy.COHORT, Reason.NO_TASTE
    match strategy:
        case Strategy.LLM:
            return model_rail(vid, taste, guides, now)
        case Strategy.COHORT:
            device, language = query.get("device", "web"), query.get("lang", "en")
            return cohort_rail(reason, taste, guides, now, device=device, language=language)
        case _:
            return answer(Strategy.BASELINE, reason, baseline(taste.genres, guides, now))


@app.get("/rail")
def rail() -> Response[dict[str, Any]]:
    started = time.perf_counter()
    claims: dict[str, Any] = app.current_event.request_context.authorizer.jwt_claim or {}
    if not isinstance(claims.get("iss"), str) or not isinstance(claims.get("sub"), str):
        return Response(401, content_types.APPLICATION_JSON, {"message": "No viewer in the token"}, headers=NO_STORE)
    query = {k: v for k, v in (app.current_event.query_string_parameters or {}).items() if isinstance(v, str)}
    served = rail_for(claims, query, datetime.now(UTC))
    with single_metric(name="Rails", unit=MetricUnit.Count, value=1, namespace=metrics.namespace) as metric:
        metric.add_dimension(name="Strategy", value=served.strategy.value)
        metric.add_dimension(name="Reason", value=served.reason.value)
    metrics.add_metric(name="RailLatencyMs", unit=MetricUnit.Milliseconds, value=(time.perf_counter() - started) * 1000)
    logger.info(
        "rail served",
        extra={"strategy": served.strategy.value, "reason": served.reason.value, "tiles": len(served.tiles)},
    )
    return Response(200, content_types.APPLICATION_JSON, served.model_dump(mode="json"), headers=NO_STORE)


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
