"""The rail recommender (``modules.recs``, M10a; ADR-0027) on a deployed stage: the demo personas get model-ranked
rails that pass the eval's checks, and a forced model failure serves the rule-based rail.

Runs only on a stage with the Recs and Identity stacks (``modules.recs``; no demand needed), the sample titles seeded
(``make seed``) and the demo users seeded (``scripts/seed_personas.py``, which also records their consents); skipped
otherwise. The module fixture schedules the four sample titles on the stage's first channel for the next hours (the
rail recommends what airs in the next day), and builds the cohort rails at once instead of waiting for the hourly run.

1. **First session:** a persona's first sign-in gets the cohort rail, re-ranked by their genres, and no model call.
2. **Second session:** their next sign-in starts a build and gets the rule-based rail (``building``); the next
   requests get the model rail. Each passes what the eval checks of a served rail: candidate titles only, reasons
   within length and free of blocked words, every claim held by the input, and a first tile in a declared genre.
   Dana and Ben, with the same genres, share one model call.
3. **Router:** a kids profile gets the kids lineup; an opted-out viewer the cohort rail (no model).
4. **Forced failures:** the ``recommender`` registry entry edited (the cap below the day's spend; an output budget
   of one token) makes the next build fail, and the viewer gets the rule-based rail with the reason (``cap``,
   ``invalid``). The entry is put back after each.
5. **Metrics:** the builder's model cost and latency reach CloudWatch (EMF).

Model cost: about ten Haiku 5.5 calls, under a cent.
"""

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import pytest
from _stack import Module, Plane, Stage
from botocore.exceptions import ClientError
from harness import BREAK_POLICY, LEAD, Channel, Tools, wait_for
from harness_identity import claims, credentials, parameter, record_consent, sign_in

from fast_channels.core.identity.names import Parameter
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.model_registry import task_parameter
from fast_channels.core.personas import PERSONAS
from fast_channels.core.recs import TASK
from fast_channels.core.recs.candidates import Taste
from fast_channels.core.recs.cards import card
from fast_channels.core.recs.rail import Daypart, airings
from fast_channels.core.recs.router import holdout_bucket
from fast_channels.core.recs.validate import BLOCKED, MIN_TILES, PII, REASON_MAX, Entities, unsupported

pytestmark = pytest.mark.e2e

SAMPLE_TITLES: Final = ("sintel", "tears-of-steel", "elephants-dream", "sintel-trailer")
LOOPS: Final = 3
"""The four sample titles three times: about two hours of guide, so every title airs in the next day."""
ADULTS: Final = ("dana", "marcus", "luis", "sam")
"""Signed-in adults with declared genres and a consent that allows personalization."""
TWIN: Final = "ben"
"""Dana's genres: the same request, so the same rail and no second model call."""
BUILD_TIMEOUT_S: Final = 120
ENTRY_CACHE_S: Final = 65
"""The builder rereads the registry entry every 60 s."""
METRICS_TIMEOUT_S: Final = 300
HOLDOUT_PERCENT: Final = 10


@dataclass
class Signed:
    persona: str
    token: str
    claims: dict[str, Any]
    vid: str

    @property
    def holdout(self) -> bool:
        return holdout_bucket(self.vid) < HOLDOUT_PERCENT


@dataclass(frozen=True)
class Rig:
    stage: Stage
    rail_url: str
    builder: str
    table: Any
    cards: dict[str, Any]
    entities: Entities
    channels: frozenset[str]


@pytest.fixture(scope="module")
def recs_stage(stage: Stage) -> Stage:
    for module in (Module.RECS, Module.IDENTITY):
        if stage.status(module) is None:
            pytest.skip(f"stage {stage.prefix} has no {module} stack (modules.recs)")
    return stage


def sign(stage: Stage, persona: str) -> Signed:
    """A new session of ``persona``: a sign-in (a new ``auth_time``), as the viewer app signs in."""
    try:
        credentials(stage, persona)
    except LookupError, ClientError:
        pytest.skip(f"{persona} is not seeded on {stage.prefix}: run scripts/seed_personas.py --prefix {stage.prefix}")
    token = sign_in(stage, persona)
    token_claims = claims(token)
    vid = viewer_id(
        stage.session.client("kms"),
        key_id=parameter(stage, Parameter.VID_KEY),
        issuer=parameter(stage, Parameter.ISSUER),
        sub=token_claims["sub"],
    )
    return Signed(persona, token, token_claims, vid)


def schedule_samples(stage: Stage, tools: Tools, channel: Channel) -> None:
    """The four sample titles, ``LOOPS`` times, from ``LEAD`` from now (skips if they are not encoded)."""
    library = stage.session.resource("dynamodb").Table(stage.output(Plane.DATA, "LibraryTableName"))
    ready = [
        t
        for t in SAMPLE_TITLES
        if (library.get_item(Key={"pk": f"ASSET#{t}", "sk": f"LADDER#{channel.ladder_version}"}).get("Item") or {}).get(
            "status"
        )
        == "READY"
    ]
    if len(ready) < MIN_TILES:
        pytest.skip(f"only {ready} of the sample titles are encoded on {stage.prefix}: run make seed")
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    plan = tools(
        "schedule___plan",
        channel=channel.name,
        picks=[t for _ in range(LOOPS) for t in ready],
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    tools("schedule___publish", plan_id=plan["plan_id"])


@pytest.fixture(scope="module")
def rig(recs_stage: Stage, tools: Tools, channel: Channel) -> Rig:
    stage = recs_stage
    schedule_samples(stage, tools, channel)
    cdn = stage.output(Plane.MEDIA, "CdnBaseUrl")

    def guide() -> list[Any] | None:
        response = httpx.get(f"{cdn}/epg/{channel.name}.json", timeout=15)
        programs = airings(channel.name, response.json()) if response.status_code == 200 else []
        upcoming = {a.content_id for a in programs if a.end > datetime.now(UTC)}
        return programs if len(upcoming) >= MIN_TILES else None

    programs = wait_for(guide, timeout=180, interval=10, what=f"the guide of {channel.name} to list the samples")
    catalog = stage.session.resource("dynamodb").Table(stage.output(Plane.DATA, "CatalogTableName"))
    cards = {}
    for content_id in {a.content_id for a in programs if a.content_id}:
        meta = catalog.get_item(Key={"pk": f"CONTENT#{content_id}", "sk": "META"}).get("Item")
        if meta:
            understood = catalog.get_item(Key={"pk": f"CONTENT#{content_id}", "sk": "UNDERSTANDING"}).get("Item")
            cards[content_id] = card(meta, understood)
    builder = stage.output(Module.RECS, "RailBuilderName")
    stage.session.client("lambda").invoke(FunctionName=builder, Payload=json.dumps({"cohorts": True}).encode())
    return Rig(
        stage=stage,
        rail_url=stage.output(Module.RECS, "RailUrl"),
        builder=builder,
        table=stage.session.resource("dynamodb").Table(stage.output(Module.RECS, "RecsTableName")),
        cards=cards,
        entities=Entities(cards.values(), {channel.name}),
        channels=frozenset({channel.name}),
    )


def ask(rig: Rig, signed: Signed, **query: str) -> dict[str, Any]:
    response = httpx.get(rig.rail_url, params=query, headers={"Authorization": f"Bearer {signed.token}"}, timeout=30)
    assert response.status_code == 200, response.text[:300]
    assert response.headers["cache-control"] == "private, no-store"
    answer: dict[str, Any] = response.json()
    return answer


def forget(rig: Rig, vid: str) -> None:
    """A viewer the recommender has never seen."""
    rig.table.delete_item(Key={"pk": f"V#{vid}"})


def eventually(rig: Rig, signed: Signed, strategy: str, *, reason: str | None = None) -> dict[str, Any]:
    def answered() -> dict[str, Any] | None:
        answer = ask(rig, signed)
        return answer if answer["strategy"] == strategy and (reason is None or answer["reason"] == reason) else None

    found: dict[str, Any] = wait_for(answered, timeout=BUILD_TIMEOUT_S, interval=5, what=f"a {strategy} rail")
    return found


def served_problems(rig: Rig, persona: str, answer: dict[str, Any]) -> list[str]:
    """What the eval's audit of a served rail finds (``functions.recs.evals.suite.audit_served``)."""
    genres = next(p.genres for p in PERSONAS if p.id == persona)

    facts = Taste(genres=genres)
    problems = []
    for tile in answer["tiles"]:
        found = rig.cards.get(tile["content_id"])
        if found is None or tile["channel"] not in rig.channels:
            problems.append(f"{tile['content_id']}: not a title of the guide")
            continue
        reason = tile["reason"]
        if BLOCKED.search(reason) or any(p.search(reason) for p in PII) or len(reason) > REASON_MAX:
            problems.append(f"{tile['content_id']}: reason {reason!r}")
        if claims_ := unsupported(reason, found, tile["channel"], facts, rig.entities):
            problems.append(f"{tile['content_id']}: unsupported {claims_}")
    if len({t["content_id"] for t in answer["tiles"]}) != len(answer["tiles"]):
        problems.append("a title appears twice")
    return problems


@pytest.fixture(scope="module")
def sessions(rig: Rig) -> Iterator[dict[str, tuple[dict[str, Any], dict[str, Any]]]]:
    """Each adult's first-session answer and their model rail from a second session."""
    answers: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for persona in (*ADULTS, TWIN):
        first = sign(rig.stage, persona)
        forget(rig, first.vid)
        before = ask(rig, first)
        time.sleep(1.1)  # auth_time has a one-second resolution
        second = sign(rig.stage, persona)
        assert second.claims["auth_time"] != first.claims["auth_time"]
        if second.holdout:
            answers[persona] = (before, ask(rig, second))
            continue
        answers[persona] = (before, eventually(rig, second, "llm"))
    yield answers


def test_a_first_session_gets_the_cohort_rail_and_buys_no_model_call(
    rig: Rig, sessions: dict[str, tuple[dict[str, Any], dict[str, Any]]]
) -> None:
    for persona, (first, _) in sessions.items():
        signed_vid = sign(rig.stage, persona).vid
        if holdout_bucket(signed_vid) < HOLDOUT_PERCENT:
            assert (first["strategy"], first["reason"]) == ("baseline", "holdout"), persona
            continue
        assert (first["strategy"], first["reason"]) == ("cohort", "first_session"), (persona, first)
        assert len(first["tiles"]) >= MIN_TILES


def test_adults_get_model_rails_that_pass_the_evals_checks(
    rig: Rig, sessions: dict[str, tuple[dict[str, Any], dict[str, Any]]]
) -> None:
    served = 0
    for persona in ADULTS:
        _, rail = sessions[persona]
        if rail["strategy"] == "baseline":
            assert rail["reason"] == "holdout", (persona, rail)
            continue
        served += 1
        assert rail["strategy"] == "llm" and rail["reason"] == "model", (persona, rail)
        assert len(rail["tiles"]) >= MIN_TILES, (persona, rail)
        assert served_problems(rig, persona, rail) == [], persona
        genres = set(next(p.genres for p in PERSONAS if p.id == persona))
        assert set(rig.cards[rail["tiles"][0]["content_id"]].genres) & genres, (persona, rail["tiles"][0])
        assert rail["lineage"]["model"].endswith("anthropic.claude-haiku-5-5")
        assert rail["lineage"]["request_id"]
    assert served >= len(ADULTS) - 1, "the holdout took more than one persona"


def test_viewers_with_the_same_genres_share_one_rail(
    sessions: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    dana, ben = sessions["dana"][1], sessions[TWIN][1]
    if "llm" not in (dana["strategy"], ben["strategy"]) or dana["strategy"] != ben["strategy"]:
        pytest.skip("one of the twins is in the holdout")
    assert dana["lineage"]["request_hash"] == ben["lineage"]["request_hash"]
    assert [t["content_id"] for t in dana["tiles"]] == [t["content_id"] for t in ben["tiles"]]


def test_a_kids_profile_gets_the_kids_lineup(rig: Rig) -> None:
    answer = ask(rig, sign(rig.stage, "marcus"), profile="kids")

    assert (answer["strategy"], answer["reason"]) == ("kids", "kids")
    assert all(rig.cards.get(t["content_id"]) is None or rig.cards[t["content_id"]].rating in {"TV-Y", "TV-Y7", "TV-G"}
               for t in answer["tiles"])  # fmt: skip


def test_an_opted_out_viewer_gets_the_cohort_rail(rig: Rig, sessions: dict[str, Any]) -> None:
    signed = sign(rig.stage, "luis")
    record_consent(rig.stage, signed.token, opt_out=True)
    try:
        answer = ask(rig, signed)
    finally:
        record_consent(rig.stage, signed.token, opt_out=False)  # Luis is PSEUDONYMOUS again

    assert (answer["strategy"], answer["reason"]) == ("cohort", "opted_out")


def test_the_cohort_rail_is_public_on_the_cdn(rig: Rig) -> None:
    cdn = rig.stage.output(Plane.MEDIA, "CdnBaseUrl")
    daypart = Daypart.at(datetime.now(UTC))
    response = httpx.get(f"{cdn}/plan/rail/web-en-{daypart}.json", timeout=15)

    assert response.status_code == 200
    doc = response.json()
    assert doc["strategy"] == "cohort" and len(doc["tiles"]) >= MIN_TILES
    assert doc["lineage"]["model"].endswith("anthropic.claude-haiku-5-5")


@pytest.fixture
def entry(rig: Rig) -> Iterator[dict[str, Any]]:
    """The ``recommender`` registry entry, put back exactly as it was after the test."""
    ssm = rig.stage.session.client("ssm")
    name = task_parameter(rig.stage.prefix, TASK)
    original = ssm.get_parameter(Name=name)["Parameter"]["Value"]
    document: dict[str, Any] = {**json.loads(original), "_name": name}
    try:
        yield document
    finally:
        ssm.put_parameter(Name=name, Type="String", Overwrite=True, Value=original)


def edit(rig: Rig, entry: dict[str, Any], **fields: Any) -> None:
    ssm = rig.stage.session.client("ssm")
    value = {k: v for k, v in entry.items() if k != "_name"} | fields
    ssm.put_parameter(Name=entry["_name"], Type="String", Overwrite=True, Value=json.dumps(value))
    time.sleep(ENTRY_CACHE_S)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [({"daily_cap_usd": 0.000001}, "cap"), ({"max_tokens": 1}, "invalid")],
    ids=["cap", "output_budget"],
)
def test_a_forced_model_failure_serves_the_rule_based_rail(
    rig: Rig, sessions: dict[str, Any], entry: dict[str, Any], fields: dict[str, Any], reason: str
) -> None:
    signed = next(s for s in (sign(rig.stage, p) for p in ADULTS) if not s.holdout)
    edit(rig, entry, **fields)
    state = rig.table.get_item(Key={"pk": f"V#{signed.vid}"}).get("Item") or {}
    if "rail_key" in state:  # the built rail too: an identical request would reuse it without a model call
        rig.table.delete_item(Key={"pk": f"REQ#{state['rail_key']}"})
    rig.table.update_item(
        Key={"pk": f"V#{signed.vid}"}, UpdateExpression="REMOVE rail_key, built_at, failure, failed_at"
    )

    answer = eventually(rig, signed, "baseline", reason=reason)

    assert len(answer["tiles"]) >= 1 and all(t["reason"].startswith("You like") for t in answer["tiles"])
    rig.table.update_item(Key={"pk": f"V#{signed.vid}"}, UpdateExpression="REMOVE failure, failed_at")


def test_the_builder_reports_model_cost_and_latency(rig: Rig, sessions: dict[str, Any]) -> None:
    cloudwatch = rig.stage.session.client("cloudwatch")

    def reported() -> bool | None:
        names = {
            m["MetricName"]
            for page in cloudwatch.get_paginator("list_metrics").paginate(
                Namespace="FastChannels", Dimensions=[{"Name": "service", "Value": "recs.builder"}]
            )
            for m in page["Metrics"]
        }
        return {"ModelCostUsd", "ModelLatencyMs", "InputTokens", "OutputTokens"} <= names or None

    wait_for(reported, timeout=METRICS_TIMEOUT_S, interval=20, what="the builder's EMF metrics")
    since = datetime.now(UTC) - timedelta(hours=1)
    points = cloudwatch.get_metric_statistics(
        Namespace="FastChannels",
        MetricName="ModelLatencyMs",
        Dimensions=[{"Name": "service", "Value": "recs.builder"}],
        StartTime=since,
        EndTime=datetime.now(UTC),
        Period=3600,
        Statistics=["Average", "SampleCount"],
    )["Datapoints"]
    assert points and points[0]["SampleCount"] >= 1
