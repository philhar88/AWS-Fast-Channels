"""The rail builder against DynamoDB Local tables, the eval fixture's guides and cards, and a stand-in model."""

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from fake_bedrock import FakeBedrock

from fast_channels.core.model_registry import TaskEntry
from fast_channels.core.recs import REGISTRY_DEFAULT
from fast_channels.core.recs.candidates import Taste
from fast_channels.core.recs.rail import Daypart
from fast_channels.core.recs.router import Strategy
from fast_channels.core.recs.serve import RailDoc
from fast_channels.functions.recs import builder, inputs, model, settings, store
from fast_channels.functions.recs.evals import fixture

if TYPE_CHECKING:
    from conftest import Tables

PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
ENTRY = TaskEntry.model_validate(
    {**REGISTRY_DEFAULT, "model": "us.anthropic.claude-haiku-5-5", "profile_arn": PROFILE, "daily_cap_usd": 2}
)
VID = "a" * 32
DANA = Taste(genres=("sci-fi", "action", "fantasy"))


class Context:
    """A Lambda context, as Powertools reads it."""

    function_name = "builder"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:builder"
    aws_request_id = "request"


class FakeS3:
    def __init__(self) -> None:
        self.put: dict[str, dict[str, Any]] = {}

    def put_object(self, **request: Any) -> dict[str, Any]:
        self.put[request["Key"]] = request
        return {}


class FakeSns:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **message: Any) -> dict[str, Any]:
        self.published.append(message)
        return {}


class Rig:
    def __init__(self, tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tables, self.s3, self.sns = tables, FakeS3(), FakeSns()
        self.bedrock = FakeBedrock()
        self.entry = ENTRY
        for name, value in {
            "RECS_TABLE": tables.recs.name,
            "MEDIA_BUCKET": "media",
            "RECS_CHILD_DIRECTED": fixture.KIDS_CHANNEL,
            "RECS_COHORTS": "web-en",
            "MODEL_COUNTER_TABLE": tables.counters.name,
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:fast-alerts",
        }.items():
            monkeypatch.setenv(name, value)
        settings.recs_settings.cache_clear()
        cards = fixture.cards()
        monkeypatch.setattr(builder, "table", lambda name: tables.recs)
        clients = {"dynamodb": tables.client, "sns": self.sns, "s3": self.s3}
        monkeypatch.setattr(builder, "client", lambda service: clients[service])
        monkeypatch.setattr(inputs, "guides", lambda channels=None: fixture.guides())
        monkeypatch.setattr(inputs, "cards", lambda ids: {i: cards[i] for i in ids if i in cards})
        monkeypatch.setattr(inputs, "task_entry", lambda: self.entry)
        monkeypatch.setattr(model, "runtime", lambda *a, **k: self.bedrock)
        monkeypatch.setattr(builder, "clock", lambda: fixture.NOW)


@pytest.fixture
def rig(tables: Tables, monkeypatch: pytest.MonkeyPatch) -> Iterator[Rig]:
    yield Rig(tables, monkeypatch)
    settings.recs_settings.cache_clear()


def test_a_viewer_rail_is_built_validated_stored_and_recorded(rig: Rig) -> None:
    store.claim_build(rig.tables.recs, VID)

    result = builder.handler({"vid": VID, "taste": DANA.model_dump(mode="json")}, Context())  # type: ignore[arg-type]

    assert result["built"] and result["tiles"] >= 3
    state = store.viewer(rig.tables.recs, VID)
    assert state.building_at is None and state.failure is None
    doc = store.get_rail(rig.tables.recs, str(state.rail_key))
    assert doc is not None and doc.strategy is Strategy.LLM
    assert doc.lineage is not None and doc.lineage.model == "us.anthropic.claude-haiku-5-5"
    assert doc.lineage.request_id == "req-1"
    request = rig.bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert VID not in request and "little-sprouts" not in request
    [counter] = rig.tables.counters.scan()["Items"]
    assert counter["pk"].startswith("COUNTER#recommender#") and Decimal(counter["spent_usd"]) > 0


def test_an_identical_request_reuses_the_rail_without_a_model_call(rig: Rig) -> None:
    builder.viewer_rail(builder.ViewerBuild(vid=VID, taste=DANA))
    builder.viewer_rail(builder.ViewerBuild(vid="b" * 32, taste=DANA))  # Ben has Dana's genres

    assert len(rig.bedrock.calls) == 1
    assert store.viewer(rig.tables.recs, VID).rail_key == store.viewer(rig.tables.recs, "b" * 32).rail_key


def test_past_the_cap_the_build_fails_with_cap_and_no_call(rig: Rig) -> None:
    rig.entry = ENTRY.model_copy(update={"daily_cap_usd": 0.000001})
    builder.viewer_rail(builder.ViewerBuild(vid=VID, taste=DANA))  # spends past the tiny cap
    store.claim_build(rig.tables.recs, "c" * 32)

    result = builder.viewer_rail(builder.ViewerBuild(vid="c" * 32, taste=Taste(genres=("documentary",))))

    assert result == {"built": False, "reason": "cap"}
    assert len(rig.bedrock.calls) == 1
    assert store.viewer(rig.tables.recs, "c" * 32).failure == "cap"
    assert len(rig.sns.published) == 1


@pytest.mark.parametrize(
    ("bedrock", "reason"),
    [
        (FakeBedrock(lambda _: "{}"), "invalid"),
        (FakeBedrock(stop_reason="max_tokens"), "invalid"),
        (FakeBedrock(lambda _: {"title": "t", "tiles": []}), "invalid"),
    ],
)
def test_a_rail_the_model_or_validator_cannot_serve_fails_with_its_reason(
    rig: Rig, bedrock: FakeBedrock, reason: str
) -> None:
    rig.bedrock = bedrock

    assert builder.viewer_rail(builder.ViewerBuild(vid=VID, taste=DANA)) == {"built": False, "reason": reason}
    assert store.viewer(rig.tables.recs, VID).failure == reason


def test_the_cohort_run_publishes_each_cohorts_rail_for_the_daypart(rig: Rig) -> None:
    results = builder.cohort_rails(now=fixture.NOW)

    assert results == {"web-en-evening": "published"}
    put = rig.s3.put["plan/rail/web-en-evening.json"]
    assert put["CacheControl"] == "public, max-age=300"
    doc = RailDoc.model_validate_json(put["Body"])
    assert doc.strategy is Strategy.COHORT and len(doc.tiles) >= 3
    assert "cohort" in json.loads(rig.bedrock.calls[0]["messages"][0]["content"][0]["text"].split("```json\n")[1].split(
        "\n```")[0])  # fmt: skip
    assert Daypart.at(fixture.NOW) is Daypart.EVENING


def test_a_request_that_is_not_a_build_is_refused(rig: Rig) -> None:
    assert builder.handler({"vid": VID}, Context()) == {"built": False, "reason": "bad_request"}  # type: ignore[arg-type]
