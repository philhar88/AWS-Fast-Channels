"""``GET /rail``'s decisions, against DynamoDB Local tables and stand-ins for S3, Lambda and the guides."""

import io
import json
from collections.abc import Iterator
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from botocore.exceptions import ClientError

from fast_channels.core.identity import consent
from fast_channels.core.recs.candidates import Cohort
from fast_channels.core.recs.rail import Daypart
from fast_channels.core.recs.router import Reason, Strategy, holdout_bucket
from fast_channels.core.recs.serve import RailDoc, cohort_key
from fast_channels.core.recs.validate import RailTile
from fast_channels.functions.recs import api, inputs, settings, store
from fast_channels.functions.recs.evals import fixture

if TYPE_CHECKING:
    from conftest import Tables

NOW = fixture.NOW
VID = next(f"{i:032x}" for i in range(1000) if holdout_bucket(f"{i:032x}") >= 10)
HOLDOUT_VID = next(f"{i:032x}" for i in range(1000) if holdout_bucket(f"{i:032x}") < 10)
DANA = {"iss": "https://issuer", "sub": "sub-dana", "fast:persona": "dana", "auth_time": 2_000}
RAIL = RailDoc(
    strategy=Strategy.LLM,
    title="Your sci-fi night",
    tiles=tuple(
        RailTile(content_id=i, channel="starlight", reason="Because you like sci-fi", evidence=("declared_genre",))
        for i in ("orbit-run", "vector-zero", "star-forge")
    ),
)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeLambda:
    def __init__(self) -> None:
        self.invoked: list[dict[str, Any]] = []

    def invoke(self, **request: Any) -> dict[str, Any]:
        self.invoked.append({**request, "Payload": json.loads(request["Payload"])})
        return {"StatusCode": 202}


class Rig:
    def __init__(self, tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tables, self.s3, self.lambda_ = tables, FakeS3(), FakeLambda()
        self.kill = False
        for name, value in {
            "RECS_TABLE": tables.recs.name,
            "IDENTITY_TABLE": tables.identity.name,
            "MEDIA_BUCKET": "media",
            "RECS_BUILDER_FUNCTION": "builder",
            "RECS_COHORTS": "web-en,tv-en",
            "RECS_CHILD_DIRECTED": fixture.KIDS_CHANNEL,
        }.items():
            monkeypatch.setenv(name, value)
        settings.recs_settings.cache_clear()
        by_name = {tables.recs.name: tables.recs, tables.identity.name: tables.identity}
        monkeypatch.setattr(api, "table", lambda name: by_name[name])
        monkeypatch.setattr(api, "client", lambda service: {"s3": self.s3, "lambda": self.lambda_}[service])
        monkeypatch.setattr(api, "caller_vid", lambda issuer, sub: HOLDOUT_VID if sub == "sub-holdout" else VID)
        monkeypatch.setattr(inputs, "guides", lambda channels=None: fixture.guides())
        monkeypatch.setattr(inputs, "kill_switch", lambda: self.kill)

    def consent(
        self, *, vid: str = VID, opt_out: bool = False, vppa: bool = True, age: timedelta = timedelta()
    ) -> None:
        row = consent.ConsentRecord(
            pk=consent.key(vid), opt_out=opt_out, vppa=vppa, updated_at=int((NOW - age).timestamp() * 1000)
        )
        self.tables.identity.put_item(Item=row.item())

    def cohort(self, daypart: Daypart = Daypart.EVENING, device: str = "web") -> None:
        extra = RailTile(content_id="lantern-kingdom", channel="toon-harbor", reason="Fantasy", evidence=("daypart",))
        doc = RailDoc(strategy=Strategy.COHORT, title="Picked for you", tiles=(*RAIL.tiles, extra))
        self.s3.objects[cohort_key(Cohort(device=device, language="en", daypart=daypart))] = (
            doc.model_dump_json().encode()
        )

    def ask(self, claims: dict[str, Any] = DANA, **query: str) -> Any:
        return api.rail_for(claims, query, NOW)


@pytest.fixture
def rig(tables: Tables, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    yield Rig(tables, monkeypatch)
    settings.recs_settings.cache_clear()


def test_the_first_session_gets_the_cohort_rail_reranked_by_declared_genres(rig: Rig) -> None:
    rig.consent()
    rig.cohort()

    served = rig.ask()

    assert (served.strategy, served.reason) == (Strategy.COHORT, Reason.FIRST_SESSION)
    assert served.tiles[0].content_id in {"orbit-run", "vector-zero", "star-forge"}  # Dana's genres first
    assert rig.lambda_.invoked == []  # a sign-up buys no model call


def test_a_later_session_builds_the_model_rail_and_then_gets_it(rig: Rig) -> None:
    rig.consent()
    store.seen(rig.tables.recs, VID, 1_000)  # the first session was earlier

    building = rig.ask()
    assert (building.strategy, building.reason) == (Strategy.BASELINE, Reason.BUILDING)
    [invoked] = rig.lambda_.invoked
    assert invoked["InvocationType"] == "Event"
    assert invoked["Payload"] == {"vid": VID, "taste": {"genres": ["sci-fi", "action", "fantasy"], "moods": [],
                                                         "rating_ceiling": "TV-MA"}}  # fmt: skip
    rig.ask()
    assert len(rig.lambda_.invoked) == 1  # one build at a time

    store.put_rail(rig.tables.recs, "hash", RAIL, hours=36, now=NOW.timestamp())
    store.built(rig.tables.recs, VID, "hash", now=NOW.timestamp())
    served = rig.ask()

    assert (served.strategy, served.reason, served.title) == (Strategy.LLM, Reason.MODEL, "Your sci-fi night")
    assert [t.content_id for t in served.tiles] == ["orbit-run", "vector-zero", "star-forge"]
    assert all(t.start >= NOW - timedelta(hours=2) and t.channel == "starlight" for t in served.tiles)


def test_a_failed_build_serves_the_rule_based_rail_with_its_reason_for_an_hour(rig: Rig) -> None:
    rig.consent()
    store.seen(rig.tables.recs, VID, 1_000)
    store.failed(rig.tables.recs, VID, "cap", now=NOW.timestamp() - 60)

    served = rig.ask()

    assert (served.strategy, served.reason) == (Strategy.BASELINE, Reason.CAP)
    assert served.tiles and all(t.reason.startswith("You like") for t in served.tiles)
    assert rig.lambda_.invoked == []


@pytest.mark.parametrize(
    ("setup", "claims", "expected"),
    [
        ({"kill": True}, DANA, (Strategy.BASELINE, Reason.KILL_SWITCH)),
        ({"consent": {"opt_out": True}}, DANA, (Strategy.COHORT, Reason.OPTED_OUT)),
        ({}, DANA, (Strategy.COHORT, Reason.NO_CONSENT)),
        ({"consent": {"age": timedelta(days=400)}}, DANA, (Strategy.COHORT, Reason.NO_CONSENT)),
        ({"consent": {"vid": HOLDOUT_VID}}, {**DANA, "sub": "sub-holdout"}, (Strategy.BASELINE, Reason.HOLDOUT)),
        ({"consent": {}}, {**DANA, "fast:persona": ""}, (Strategy.COHORT, Reason.NO_TASTE)),
    ],
)
def test_the_router_runs_before_any_model(
    rig: Rig, setup: dict[str, Any], claims: dict[str, Any], expected: tuple[Strategy, Reason]
) -> None:
    rig.kill = setup.get("kill", False)
    if "consent" in setup:
        rig.consent(**setup["consent"])
    store.seen(rig.tables.recs, HOLDOUT_VID if claims["sub"] == "sub-holdout" else VID, 1_000)
    rig.cohort()

    served = rig.ask(claims)

    assert (served.strategy, served.reason) == expected
    assert rig.lambda_.invoked == []


def test_a_missing_cohort_rail_serves_the_rule_based_rail(rig: Rig) -> None:
    served = rig.ask()  # no consent: the cohort rail, which is not built yet
    assert (served.strategy, served.reason) == (Strategy.BASELINE, Reason.UNAVAILABLE)


def test_the_cohort_follows_the_device(rig: Rig) -> None:
    rig.cohort(device="tv")
    assert rig.ask(device="tv").strategy is Strategy.COHORT
    assert rig.ask(device="phone").reason is Reason.UNAVAILABLE  # unknown devices get the first cohort, web-en


def test_a_kids_profile_gets_the_kids_lineup(rig: Rig) -> None:
    served = rig.ask(profile="kids")

    assert (served.strategy, served.reason) == (Strategy.KIDS, Reason.KIDS)
    assert served.tiles and {t.channel for t in served.tiles} <= {fixture.KIDS_CHANNEL, "toon-harbor", "wide-world",
                                                                  "laugh-lines"}  # fmt: skip
    assert all(t.content_id not in {"orbit-run", "night-crawlers"} for t in served.tiles)


def test_rail_answers_no_store_and_refuses_a_token_without_a_viewer(rig: Rig) -> None:
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": "GET /rail",
        "rawPath": "/rail",
        "rawQueryString": "profile=kids",
        "queryStringParameters": {"profile": "kids"},
        "headers": {},
        "requestContext": {
            "http": {"method": "GET", "path": "/rail"},
            "authorizer": {"jwt": {"claims": DANA, "scopes": ["fast/playback"]}},
            "requestId": "r",
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }

    answered = api.app.resolve(event, None)  # type: ignore[arg-type]
    refused = api.app.resolve({**event, "requestContext": {**event["requestContext"], "authorizer": {"jwt": {
        "claims": {}, "scopes": []}}}}, None)  # type: ignore[arg-type]  # fmt: skip

    assert answered["statusCode"] == 200
    assert answered["headers"]["Cache-Control"] == "private, no-store"
    assert json.loads(answered["body"])["strategy"] == "kids"
    assert refused["statusCode"] == 401
