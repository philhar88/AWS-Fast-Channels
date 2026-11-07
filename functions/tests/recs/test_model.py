from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from fake_bedrock import USAGE, FakeBedrock

from fast_channels.core.model_registry import TaskSettings
from fast_channels.core.model_spend import CapReached, cost
from fast_channels.core.recs import REGISTRY_DEFAULT
from fast_channels.core.recs.router import Reason
from fast_channels.functions.recs import model

SETTINGS = TaskSettings.model_validate({**REGISTRY_DEFAULT, "model": "us.anthropic.claude-haiku-5-5"})
PROFILE = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abc123"
RAIL = {"title": "Picked for you", "tiles": [{"content_id": "a", "reason": "Sci-fi", "evidence": ["declared_genre"]}]}


def call(client: Any, **kw: Any) -> model.ModelRail:
    return model.call(SETTINGS, PROFILE, catalog="CATALOG", request="REQUEST", client=client, **kw)


class Counter:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.added: list[Decimal] = []

    def admit(self, task: str, cap: Decimal) -> None:
        if self.refuse:
            raise CapReached(task, cap)

    def add(self, task: str, usd: Decimal) -> None:
        self.added.append(usd)


def test_a_rail_is_parsed_and_priced_by_the_registry_model() -> None:
    client, counter = FakeBedrock(lambda _: RAIL), Counter()

    rail = call(client, counter=counter, cap_usd=Decimal(2))  # type: ignore[arg-type]

    assert rail.output.tiles[0].content_id == "a"
    assert rail.cost_usd == cost("us.anthropic.claude-haiku-5-5", USAGE)
    assert counter.added == [rail.cost_usd]
    assert (rail.latency_ms, rail.request_id) == (4_600, "req-1")
    assert client.calls[0]["modelId"] == PROFILE


def test_the_cap_refuses_before_any_call() -> None:
    client = FakeBedrock(lambda _: RAIL)

    with pytest.raises(model.ModelFailure) as failure:
        call(client, counter=Counter(refuse=True), cap_usd=Decimal(2))  # type: ignore[arg-type]

    assert failure.value.reason is Reason.CAP
    assert client.calls == []


def raise_(error: Exception) -> Any:
    def answer(_: dict[str, Any]) -> Any:
        raise error

    return answer


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (FakeBedrock(raise_(ReadTimeoutError(endpoint_url="https://bedrock"))), Reason.TIMEOUT),
        (
            FakeBedrock(raise_(ClientError({"Error": {"Code": "AccessDeniedException"}}, "Converse"))),
            Reason.MODEL_ERROR,
        ),
        (FakeBedrock(raise_(ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse"))), Reason.MODEL_ERROR),
        (FakeBedrock(lambda _: RAIL, stop_reason="max_tokens"), Reason.INVALID),
        (FakeBedrock(lambda _: "not json"), Reason.INVALID),
        (FakeBedrock(lambda _: {"title": "t", "tiles": [{"content_id": "a"}]}), Reason.INVALID),
    ],
)
def test_every_failure_has_its_fallback_reason(client: FakeBedrock, reason: Reason) -> None:
    with pytest.raises(model.ModelFailure) as failure:
        call(client)
    assert failure.value.reason is reason
