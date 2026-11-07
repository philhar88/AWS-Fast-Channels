"""The rail builder: the only function that calls the ``recommender`` model (DESIGN §15 pipeline, no request waits).

Two triggers:

- ``{"vid": ..., "taste": {...}}`` from the rail API, asynchronously, when a viewer's model rail is missing or older
  than ``refresh_hours``. The taste facts come with the event; the ``vid`` is only the key the rail is stored
  under and never reaches the prompt.
- ``{"cohorts": true}`` hourly from EventBridge Scheduler: one cohort rail per configured ``<device>-<language>`` for
  the current daypart, written to ``plan/rail/<device>-<language>-<daypart>.json`` in the media bucket, where guests'
  apps read it through the CDN.

Each build: the next day of the guides, the cards of the titles in it, the candidates, then the request hash. An
identical request built in the last ``rail_hours`` is reused without a model call; otherwise the model is called
under the task's daily cap and its rail validated. A viewer's failed build is recorded with its reason, and the rail
API serves the rule-based rail with that reason for an hour before it tries again.
"""

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger, Metrics, single_metric
from aws_lambda_powertools.metrics import MetricUnit
from pydantic import BaseModel, ConfigDict, ValidationError

from fast_channels.core.model_spend import SpendCounter
from fast_channels.core.recs import TASK
from fast_channels.core.recs.candidates import Cohort, Taste, candidates, catalog_cards
from fast_channels.core.recs.cards import catalog_version
from fast_channels.core.recs.prompt import PROMPT_VERSION, SCHEMA_VERSION, catalog_block, request_block, request_hash
from fast_channels.core.recs.rail import Daypart
from fast_channels.core.recs.router import Reason, Strategy
from fast_channels.core.recs.serve import Lineage, RailDoc, cohort_key
from fast_channels.core.recs.validate import MIN_TILES, audit_prompt, validate
from fast_channels.functions.aws import client, table
from fast_channels.functions.recs import inputs, model, store
from fast_channels.functions.recs.settings import recs_settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()
metrics = Metrics()

COHORT_CACHE_CONTROL: Final = "public, max-age=300"
"""Guests' apps reread the cohort rail every five minutes at most; it changes at most hourly."""


def clock() -> datetime:
    """Now (UTC); tests replace it."""
    return datetime.now(UTC)


class ViewerBuild(BaseModel):
    """The rail API's request for one viewer's model rail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vid: str
    taste: Taste


class BuildFailure(Exception):  # noqa: N818 - the reason a rail falls back
    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def build(
    facts: Taste | Cohort, *, forbidden: tuple[str, ...] = (), now: datetime | None = None
) -> tuple[str, RailDoc]:
    """Build (or reuse) the rail for ``facts``; returns its request hash and document.

    Raises:
        BuildFailure: too few candidates, an audit hit, a model failure or an invalid rail.
    """
    config = recs_settings()
    now = now or clock()
    guides = inputs.guides()
    window = {a.content_id for g in guides for a in g if a.content_id}
    all_cards = inputs.cards(window)
    child_directed = set(config.child_directed)
    allowed = candidates(all_cards, guides, now, facts, child_directed=child_directed)
    if len(allowed) < MIN_TILES:
        raise BuildFailure(Reason.INVALID, f"{len(allowed)} candidates in the next day")
    shown = catalog_cards(all_cards, guides, now, ceiling=facts.rating_ceiling, child_directed=child_directed)
    catalog, request = catalog_block(shown), request_block(facts, allowed)
    if hits := audit_prompt(request, forbidden):
        raise BuildFailure(Reason.INVALID, f"the request held {len(hits)} forbidden strings")
    entry = inputs.task_entry()
    key = request_hash(model=entry.model, catalog=catalog, request=request)
    recs = table(config.table)
    if (reused := store.get_rail(recs, key)) is not None:
        metrics.add_metric(name="RailsReused", unit=MetricUnit.Count, value=1)
        return key, reused
    counter = SpendCounter(
        table=config.model_counter_table,
        topic_arn=config.alert_topic_arn,
        dynamodb=client("dynamodb"),
        sns=client("sns"),
    )
    cap = Decimal(str(entry.daily_cap_usd)) if entry.daily_cap_usd is not None else None
    try:
        rail = model.call(
            entry,
            entry.profile_arn,
            catalog=catalog,
            request=request,
            client=model.runtime(),
            counter=counter,
            cap_usd=cap,
        )
    except model.ModelFailure as failure:
        if failure.cost_usd:
            metrics.add_metric(name="ModelCostUsd", unit=MetricUnit.NoUnit, value=float(failure.cost_usd))
        raise BuildFailure(failure.reason, failure.detail) from None
    _model_metrics(rail)
    verdict = validate(
        rail.output.model_dump(mode="json"), candidates=allowed, cards=all_cards, facts=facts, forbidden=forbidden
    )
    metrics.add_metric(name="ValidatorDrops", unit=MetricUnit.Count, value=sum(verdict.dropped.values()))
    metrics.add_metric(name="ReasonsReplaced", unit=MetricUnit.Count, value=sum(t.replaced for t in verdict.tiles))
    metrics.add_metric(name="PolicyViolations", unit=MetricUnit.Count, value=sum(verdict.violations.values()))
    if verdict.checked:
        metrics.add_metric(name="FaithfulShare", unit=MetricUnit.NoUnit, value=verdict.faithful / verdict.checked)
    logger.info(
        "rail validated",
        extra={"dropped": dict(verdict.dropped), "violations": dict(verdict.violations), "tiles": len(verdict.tiles)},
    )
    if not verdict.valid:
        raise BuildFailure(Reason.INVALID, f"{len(verdict.tiles)} tiles passed the validator")
    doc = RailDoc(
        strategy=Strategy.COHORT if isinstance(facts, Cohort) else Strategy.LLM,
        title=verdict.title,
        tiles=tuple(verdict.tiles),
        lineage=Lineage(
            task=TASK,
            model=entry.model,
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            catalog_version=catalog_version(shown),
            request_hash=key,
            request_id=rail.request_id,
            built_at=now,
            cost_usd=str(rail.cost_usd),
            latency_ms=rail.latency_ms,
        ),
    )
    store.put_rail(recs, key, doc, hours=config.rail_hours)
    return key, doc


def failure_metric(reason: Reason) -> None:
    """``RailBuildFailures``, with the failure's ``Reason`` as a dimension."""
    with single_metric(name="RailBuildFailures", unit=MetricUnit.Count, value=1, namespace=metrics.namespace) as m:
        m.add_dimension(name="Reason", value=reason.value)


def _model_metrics(rail: model.ModelRail) -> None:
    metrics.add_metric(name="ModelLatencyMs", unit=MetricUnit.Milliseconds, value=rail.latency_ms)
    metrics.add_metric(name="ModelCostUsd", unit=MetricUnit.NoUnit, value=float(rail.cost_usd))
    for usage, name in (
        ("inputTokens", "InputTokens"),
        ("outputTokens", "OutputTokens"),
        ("cacheReadInputTokens", "CacheReadTokens"),
        ("cacheWriteInputTokens", "CacheWriteTokens"),
    ):
        metrics.add_metric(name=name, unit=MetricUnit.Count, value=rail.usage.get(usage, 0))


def viewer_rail(request: ViewerBuild) -> dict[str, Any]:
    """Build one viewer's model rail and record the outcome on their state item."""
    config = recs_settings()
    recs = table(config.table)
    started = time.perf_counter()
    try:
        key, doc = build(request.taste, forbidden=(request.vid,))
    except BuildFailure as failure:
        logger.warning("viewer rail failed", extra={"reason": failure.reason.value, "detail": failure.detail})
        failure_metric(failure.reason)
        store.failed(recs, request.vid, failure.reason.value)
        return {"built": False, "reason": failure.reason.value}
    store.built(recs, request.vid, key)
    metrics.add_metric(name="RailBuildMs", unit=MetricUnit.Milliseconds, value=(time.perf_counter() - started) * 1000)
    metrics.add_metric(name="RailsBuilt", unit=MetricUnit.Count, value=1)
    return {"built": True, "tiles": len(doc.tiles)}


def cohort_rails(now: datetime | None = None) -> dict[str, Any]:
    """Build each configured cohort's rail for the current daypart and publish it to the media bucket."""
    config = recs_settings()
    now = now or clock()
    results: dict[str, Any] = {}
    for spec in config.cohorts:
        device, _, language = spec.partition("-")
        cohort = Cohort(device=device, language=language, daypart=Daypart.at(now))
        try:
            _, doc = build(cohort, now=now)
        except BuildFailure as failure:
            logger.warning("cohort rail failed", extra={"cohort": cohort.key, "reason": failure.reason.value})
            failure_metric(failure.reason)
            results[cohort.key] = failure.reason.value
            continue
        client("s3").put_object(
            Bucket=config.media_bucket,
            Key=cohort_key(cohort),
            Body=doc.model_dump_json().encode(),
            ContentType="application/json",
            CacheControl=COHORT_CACHE_CONTROL,
        )
        results[cohort.key] = "published"
    return results


@metrics.log_metrics
@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    if event.get("cohorts"):
        return cohort_rails()
    try:
        request = ViewerBuild.model_validate(event)
    except ValidationError:
        logger.error("not a rail build request", extra={"keys": sorted(event)})
        return {"built": False, "reason": "bad_request"}
    return viewer_rail(request)
