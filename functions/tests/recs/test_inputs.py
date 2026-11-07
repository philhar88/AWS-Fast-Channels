import io
import json
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from aws_lambda_powertools.utilities import parameters
from botocore.exceptions import ClientError

from fast_channels.core.model_registry import TaskEntry
from fast_channels.core.recs import REGISTRY_DEFAULT
from fast_channels.functions.recs import inputs, settings

if TYPE_CHECKING:
    from conftest import Tables

PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
DEPLOYED = TaskEntry.model_validate(
    {**REGISTRY_DEFAULT, "model": "us.anthropic.claude-haiku-5-5", "profile_arn": PROFILE}
)


class FakeSsm:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get_parameter(self, *, Name: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.value}}


class FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MODEL_TASK_DEFAULT", DEPLOYED.model_dump_json())
    monkeypatch.setenv("MODEL_TASK_PARAMETER", "/fast/models/tasks/recommender")
    monkeypatch.setenv("MEDIA_BUCKET", "media")
    monkeypatch.setenv("RECS_CHANNELS", "fast-demo,fast-anim")
    settings.recs_settings.cache_clear()
    inputs._cache.clear()
    yield
    settings.recs_settings.cache_clear()
    inputs._cache.clear()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (DEPLOYED.model_copy(update={"daily_cap_usd": 0.01}).model_dump_json(), 0.01),  # an operator's cap edit
        (DEPLOYED.model_copy(update={"profile_arn": PROFILE.replace("abc123", "other1")}).model_dump_json(), 2),
        ("{not json", 2),
        (None, 2),
    ],
)
def test_the_registry_entry_falls_back_to_the_deployed_one(
    monkeypatch: pytest.MonkeyPatch, stored: str | None, expected: float
) -> None:
    monkeypatch.setattr(inputs, "client", lambda service: FakeSsm(stored))

    entry = inputs.task_entry()

    assert entry.daily_cap_usd == expected
    assert entry.profile_arn == PROFILE


def test_guides_are_read_in_channel_order_and_a_missing_one_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    program = {
        "name": "p0",
        "title": "Sintel",
        "content_id": "sintel",
        "asset_id": "sintel",
        "start": "2026-11-10T19:00:00Z",
        "end": "2026-11-10T19:15:00Z",
        "rating": "TV-14",
        "genres": ["animation"],
        "breaks": [],
        "status": "PUBLISHED",
    }
    s3 = FakeS3({"epg/fast-anim.json": json.dumps({"channel": "fast-anim", "programs": [program]}).encode()})
    monkeypatch.setattr(inputs, "client", lambda service: s3)

    demo, anim = inputs.guides()

    assert demo == []
    assert [(a.channel, a.content_id) for a in anim] == [("fast-anim", "sintel")]


def test_cards_join_the_catalog_item_and_its_understanding(tables: Tables, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = f"catalog-{uuid.uuid4().hex[:8]}"
    tables.client.create_table(
        TableName=catalog,
        AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"} for n in ("pk", "sk")],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        BillingMode="PAY_PER_REQUEST",
    )
    items = [
        {"pk": {"S": "CONTENT#sintel"}, "sk": {"S": "META"}, "content_id": {"S": "sintel"}, "title": {"S": "Sintel"},
         "genres": {"L": [{"S": "animation"}, {"S": "fantasy"}]}, "duration_ms": {"N": "888000"}},
        {"pk": {"S": "CONTENT#sintel"}, "sk": {"S": "UNDERSTANDING"}, "v": {"N": "1"}, "model": {"S": "m"},
         "moods": {"L": [{"S": "epic"}]}, "cat": {"L": [{"S": "335"}]}},
        {"pk": {"S": "CONTENT#tears"}, "sk": {"S": "META"}, "content_id": {"S": "tears"}, "title": {"S": "Tears"}},
    ]  # fmt: skip
    for item in items:
        tables.client.put_item(TableName=catalog, Item=item)
    monkeypatch.setenv("CATALOG_TABLE", catalog)
    settings.recs_settings.cache_clear()
    monkeypatch.setattr(inputs, "client", lambda service: tables.client)

    cards = inputs.cards(["sintel", "tears", "unknown"])

    assert sorted(cards) == ["sintel", "tears"]
    assert (cards["sintel"].moods, cards["sintel"].categories, cards["sintel"].minutes) == (("epic",), ("Fantasy",), 15)
    assert cards["tears"].moods == ()
    tables.client.delete_table(TableName=catalog)


@pytest.mark.parametrize(
    ("flags", "killed"),
    [
        ({"recs": {"enabled": True, "strategy": "popularity"}}, True),
        ({"recs": {"enabled": True, "strategy": "recommender"}}, False),
        (None, False),  # unreadable: the recommender runs
    ],
)
def test_the_kill_switch_is_the_recs_flag(
    monkeypatch: pytest.MonkeyPatch, flags: dict[str, Any] | None, killed: bool
) -> None:
    monkeypatch.setenv("APPCONFIG_APPLICATION", "fast-fast")
    monkeypatch.setenv("APPCONFIG_ENVIRONMENT", "fast")
    settings.recs_settings.cache_clear()

    def read(**_: Any) -> Any:
        if flags is None:
            raise parameters.exceptions.GetParameterError("no")
        return flags

    monkeypatch.setattr(parameters, "get_app_config", read)

    assert inputs.kill_switch() is killed


def test_without_ops_there_is_no_kill_switch() -> None:
    assert inputs.kill_switch() is False


def test_a_guide_that_cannot_be_read_leaves_its_channel_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A channel created since the last publish has no guide; without s3:ListBucket that is a 403, not a 404. Either
    way one channel never fails the rail (fastm, 2026-11-07)."""

    class Denied(FakeS3):
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(inputs, "client", lambda service: Denied({}))

    assert inputs.guides() == [[], []]
    assert "guide unreadable, left out" in caplog.text
