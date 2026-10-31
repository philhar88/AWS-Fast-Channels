import asyncio
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import ANY, Stubber
from strands.hooks import HookRegistry

import fast_channels.agent.caps as caps_module
from fast_channels.agent.caps import CAP_REACHED, CapGuard, CapReachedError, SpendCounter, cost, price

TABLE, TOPIC = "fast-models", "arn:aws:sns:us-east-1:123456789012:fast-alerts"
NOW = 1_793_232_000.0  # 2026-10-29T00:00:00Z
KEY = {"pk": {"S": "COUNTER#scheduler.plan#2026-10-29"}}
CAP = Decimal("0.01")
EXPIRES = str(int(NOW) + 35 * 86_400)


def test_list_prices_by_route() -> None:
    assert price("us.anthropic.claude-haiku-5-5").input == Decimal("0.110")
    assert price("global.anthropic.claude-haiku-5-5").output == Decimal("0.50")
    assert price("eu.anthropic.claude-sonnet-5-5").cache_write == Decimal("2.750")
    assert price("anthropic.claude-haiku-5-5").input == Decimal("0.110")  # in-Region


def test_unknown_models_count_at_the_highest_price(caplog: pytest.LogCaptureFixture) -> None:
    assert price("us.example.model-v1") == price("us.anthropic.claude-opus-5-5")
    assert "no list price" in caplog.text


def test_cost_of_a_call_counts_cache_tokens_at_their_own_prices() -> None:
    usage = {
        "inputTokens": 10_000,
        "outputTokens": 1_000,
        "cacheReadInputTokens": 50_000,
        "cacheWriteInputTokens": 20_000,
    }

    assert cost("us.anthropic.claude-haiku-5-5", usage) == (
        Decimal(10_000) * Decimal("0.110")
        + Decimal(1_000) * Decimal("0.550")
        + Decimal(50_000) * Decimal("0.0110")
        + Decimal(20_000) * Decimal("0.13750")
    ) / Decimal(1_000_000)


@pytest.fixture
def clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Stubber, Stubber]]:
    credentials = {"region_name": "us-east-1", "aws_access_key_id": "x", "aws_secret_access_key": "x"}
    dynamodb, sns = boto3.client("dynamodb", **credentials), boto3.client("sns", **credentials)
    monkeypatch.setattr(caps_module, "_dynamodb", lambda region: dynamodb)
    monkeypatch.setattr(caps_module, "_sns", lambda region: sns)
    with Stubber(dynamodb) as db, Stubber(sns) as topic:
        yield db, topic
        db.assert_no_pending_responses()
        topic.assert_no_pending_responses()


def counter(topic_arn: str | None = TOPIC) -> SpendCounter:
    return SpendCounter(table=TABLE, topic_arn=topic_arn, region="us-east-1", clock=lambda: NOW)


def admission(counter_name: str = "calls") -> dict[str, Any]:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "ADD #counter :one SET expires_at = if_not_exists(expires_at, :expires)",
        "ConditionExpression": "attribute_not_exists(spent_usd) OR spent_usd < :cap",
        "ExpressionAttributeNames": {"#counter": counter_name},
        "ExpressionAttributeValues": {":one": {"N": "1"}, ":expires": {"N": EXPIRES}, ":cap": {"N": "0.01"}},
    }


def test_admission_under_the_cap_counts_the_run(clients: tuple[Stubber, Stubber]) -> None:
    db, _ = clients
    db.add_response("update_item", {}, admission("runs"))

    counter().admit("scheduler.plan", CAP, counter="runs")


def test_past_the_cap_the_run_is_refused_and_the_topic_told_once(clients: tuple[Stubber, Stubber]) -> None:
    db, topic = clients
    for first in (True, False):
        db.add_client_error("update_item", "ConditionalCheckFailedException", expected_params=admission())
        notified = {
            "TableName": TABLE,
            "Key": KEY,
            "UpdateExpression": "SET notified_at = :now",
            "ConditionExpression": "attribute_not_exists(notified_at)",
            "ExpressionAttributeValues": {":now": {"N": str(int(NOW))}},
        }
        if first:
            db.add_response("update_item", {}, notified)
            topic.add_response("publish", {"MessageId": "m-1"}, {"TopicArn": TOPIC, "Subject": ANY, "Message": ANY})
        else:
            db.add_client_error("update_item", "ConditionalCheckFailedException", expected_params=notified)

    for _ in range(2):
        with pytest.raises(CapReachedError) as refused:
            counter().admit("scheduler.plan", CAP)
        assert refused.value.code == CAP_REACHED
        assert "0.01 US dollars" in refused.value.message


def test_an_unreachable_counter_lets_the_run_through(
    clients: tuple[Stubber, Stubber], caplog: pytest.LogCaptureFixture
) -> None:
    db, _ = clients
    db.add_client_error("update_item", "ProvisionedThroughputExceededException")
    db.add_client_error("update_item", "AccessDeniedException")

    counter().admit("scheduler.plan", CAP)
    counter().add("scheduler.plan", Decimal("0.002"))

    assert caplog.text.count("model spend counter unavailable") == 2


def test_spend_is_added_per_call(clients: tuple[Stubber, Stubber]) -> None:
    db, _ = clients
    db.add_response(
        "update_item",
        {},
        {
            "TableName": TABLE,
            "Key": KEY,
            "UpdateExpression": "ADD spent_usd :usd SET expires_at = if_not_exists(expires_at, :expires)",
            "ExpressionAttributeValues": {":usd": {"N": "0.0042"}, ":expires": {"N": EXPIRES}},
        },
    )

    counter().add("scheduler.plan", Decimal("0.0042"))
    counter().add("scheduler.plan", Decimal(0))  # nothing to add, no call


def test_cap_guard_admits_each_call_and_counts_its_cost() -> None:
    calls: list[tuple[str, object]] = []

    class Counter:
        def admit(self, task: str, cap: Decimal) -> None:
            calls.append(("admit", cap))
            if len(calls) > 2:
                raise CapReachedError(task, cap)

        def add(self, task: str, usd: Decimal) -> None:
            calls.append(("add", usd))

    guard = CapGuard(Counter(), task="scheduler.plan", cap_usd=CAP, model_id="us.anthropic.claude-haiku-5-5")  # type: ignore[arg-type]
    before = SimpleNamespace(cancel=False)
    usage = {"inputTokens": 1_000_000, "outputTokens": 0}
    after = SimpleNamespace(stop_response=SimpleNamespace(message={"metadata": {"usage": usage}}))

    asyncio.run(guard._before_model_call(before))  # type: ignore[arg-type]
    asyncio.run(guard._after_model_call(after))  # type: ignore[arg-type]
    asyncio.run(guard._after_model_call(SimpleNamespace(stop_response=None)))  # type: ignore[arg-type]
    asyncio.run(guard._before_model_call(before))  # type: ignore[arg-type]

    assert calls == [("admit", CAP), ("add", Decimal("0.11")), ("admit", CAP)]
    assert isinstance(before.cancel, str) and "reached its cap" in before.cancel


class _Unreachable:
    """A client whose every call fails before reaching AWS."""

    def __getattr__(self, name: str) -> Any:
        def fail(**_: Any) -> Any:
            raise EndpointConnectionError(endpoint_url="https://dynamodb.us-east-1.amazonaws.com")

        return fail


def test_a_counter_that_cannot_be_reached_never_blocks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(caps_module, "_dynamodb", lambda region: _Unreachable())

    counter().admit("scheduler.plan", CAP)
    counter().add("scheduler.plan", Decimal("0.01"))

    assert caplog.text.count("model spend counter unavailable") == 2


def notified_once() -> dict[str, Any]:
    return {
        "TableName": TABLE,
        "Key": KEY,
        "UpdateExpression": "SET notified_at = :now",
        "ConditionExpression": "attribute_not_exists(notified_at)",
        "ExpressionAttributeValues": {":now": {"N": str(int(NOW))}},
    }


def test_without_a_topic_the_cap_is_only_logged(
    clients: tuple[Stubber, Stubber], caplog: pytest.LogCaptureFixture
) -> None:
    db, _ = clients
    db.add_client_error("update_item", "ConditionalCheckFailedException", expected_params=admission())
    db.add_response("update_item", {}, notified_once())

    with pytest.raises(CapReachedError):
        counter(topic_arn=None).admit("scheduler.plan", CAP)

    assert "model spend cap reached" in caplog.text


def test_a_failed_notification_still_refuses_the_run(
    clients: tuple[Stubber, Stubber], caplog: pytest.LogCaptureFixture
) -> None:
    db, topic = clients
    db.add_client_error("update_item", "ConditionalCheckFailedException", expected_params=admission())
    db.add_response("update_item", {}, notified_once())
    topic.add_client_error("publish", "AuthorizationError")
    db.add_client_error("update_item", "ConditionalCheckFailedException", expected_params=admission())
    db.add_client_error("update_item", "InternalServerError", expected_params=notified_once())

    for _ in range(2):
        with pytest.raises(CapReachedError):
            counter().admit("scheduler.plan", CAP)

    assert "cap notification could not be published" in caplog.text
    assert "cap notification skipped" in caplog.text


def test_cap_guard_registers_on_every_model_call() -> None:
    registry = HookRegistry()

    CapGuard(counter(), task="scheduler.plan", cap_usd=CAP, model_id="m").register_hooks(registry)

    assert registry.has_callbacks()
