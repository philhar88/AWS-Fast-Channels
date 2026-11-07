from decimal import Decimal
from typing import Any

import pytest

from fast_channels.core.model_spend import CAP_REACHED, CapReached, SpendCounter, cost, price

TABLE, TOPIC = "fast-models", "arn:aws:sns:us-east-1:123456789012:fast-alerts"
NOW = 1_793_232_000.0  # 2026-10-29T00:00:00Z
CAP = Decimal("0.01")


class AwsError(Exception):
    """A botocore ClientError's shape: the code is in ``response``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeTable:
    """A DynamoDB client whose ``update_item`` calls are recorded and answered from ``answers`` in order."""

    def __init__(self, *answers: Exception | None) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def update_item(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        answer = self.answers.pop(0) if self.answers else None
        if answer is not None:
            raise answer
        return {}


class FakeTopic:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(self, **message: Any) -> dict[str, Any]:
        self.published.append(message)
        return {"MessageId": "m-1"}


def counter(table: FakeTable, topic: FakeTopic | None = None) -> SpendCounter:
    return SpendCounter(table=TABLE, topic_arn=TOPIC if topic else None, dynamodb=table, sns=topic, clock=lambda: NOW)


def test_prices_by_route() -> None:
    assert price("global.anthropic.claude-haiku-5-5").input == Decimal("0.10")
    assert price("us.anthropic.claude-haiku-5-5").input == Decimal("0.110")
    assert price("us.amazon.nova-2-lite-v1:0").output == Decimal("2.750")
    assert price("global.amazon.nova-2-lite-v1:0").cache_read == Decimal("0.075")


def test_unknown_models_count_at_the_highest_price(caplog: pytest.LogCaptureFixture) -> None:
    assert price("us.example.model-v1") == price("us.anthropic.claude-opus-5-5")
    assert "no list price" in caplog.text


def test_cost_counts_cache_tokens_at_their_own_prices() -> None:
    usage = {"inputTokens": 1_000_000, "outputTokens": 100_000, "cacheReadInputTokens": 1_000_000}
    assert cost("global.anthropic.claude-haiku-5-5", usage) == Decimal("0.10") + Decimal("0.05") + Decimal("0.01")


def test_admission_counts_the_call_under_the_cap() -> None:
    table = FakeTable()

    counter(table).admit("recommender", CAP)

    [call] = table.calls
    assert call["Key"] == {"pk": {"S": "COUNTER#recommender#2026-10-29"}}
    assert call["ExpressionAttributeNames"] == {"#counter": "calls"}
    assert call["ExpressionAttributeValues"][":cap"] == {"N": "0.01"}
    assert counter(table).key("recommender") == "COUNTER#recommender#2026-10-29"


def test_past_the_cap_the_call_is_refused_and_the_topic_told_once() -> None:
    refused = AwsError("ConditionalCheckFailedException")
    table, topic = FakeTable(refused, None, refused, refused), FakeTopic()

    for _ in range(2):
        with pytest.raises(CapReached) as error:
            counter(table, topic).admit("recommender", CAP)
        assert error.value.task == "recommender"
        assert "0.01 US dollars" in error.value.message

    assert len(topic.published) == 1
    assert "recommender" in topic.published[0]["Subject"]
    assert CAP_REACHED == "MODEL_CAP_REACHED"


@pytest.mark.parametrize("failure", [AwsError("AccessDeniedException"), ConnectionError("no route")])
def test_a_counter_that_fails_lets_the_call_through(failure: Exception, caplog: pytest.LogCaptureFixture) -> None:
    table = FakeTable(failure, failure)

    counter(table).admit("recommender", CAP)
    counter(table).add("recommender", Decimal("0.002"))

    assert caplog.text.count("model spend counter unavailable") == 2


def test_spend_is_added_and_nothing_is_written_for_zero() -> None:
    table = FakeTable()

    counter(table).add("recommender", Decimal("0.0042"))
    counter(table).add("recommender", Decimal(0))

    [call] = table.calls
    assert call["ExpressionAttributeValues"][":usd"] == {"N": "0.0042"}
