import json
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

import fast_channels.agent.model as model_module
from fast_channels.agent.model import ModelSpec, ModelSpecSource, bedrock_model, parse_spec
from fast_channels.agent.protocol import Mode
from fast_channels.agent.settings import Settings

HAIKU = "us.anthropic.claude-haiku-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"
NOVA = "us.amazon.nova-2-lite-v1:0"


def test_parse_spec_accepts_allowed_models() -> None:
    spec = parse_spec(
        json.dumps({"model_id": SONNET, "effort": {"chat": "low", "plan": "high"}, "max_tokens": 8000}),
        allowed=frozenset({HAIKU, SONNET}),
    )

    assert spec == ModelSpec.model_validate(
        {"model_id": SONNET, "effort": {"chat": "low", "plan": "high"}, "max_tokens": 8000}
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"model_id": NOVA}),  # not in the allow-list
        json.dumps({"model_id": HAIKU, "provider": "openai"}),  # runtime-mutable spec is Bedrock-only
        json.dumps({"model_id": HAIKU, "base_url": "https://evil.example"}),
        json.dumps({"model_id": HAIKU, "max_tokens": 0}),
        json.dumps({"model_id": HAIKU, "effort": {"chat": "extreme"}}),
    ],
)
def test_parse_spec_rejects_invalid_specs(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_spec(raw, allowed=frozenset({HAIKU}))


def settings(**overrides: Any) -> Settings:
    return Settings(
        region="us-east-1",
        gateway_url="https://g/mcp",
        default_model_id=HAIKU,
        allowed_models=frozenset({HAIKU, SONNET}),
        **overrides,
    )


def test_default_spec_without_parameter() -> None:
    assert ModelSpecSource(settings()).current() == ModelSpec(model_id=HAIKU)


@pytest.fixture
def ssm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    client = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(model_module, "_ssm", lambda region: client)
    with Stubber(client) as stubber:
        yield stubber


def parameter(value: str) -> dict[str, Any]:
    return {"Parameter": {"Name": "/fast/model", "Type": "String", "Value": value}}


def test_spec_from_ssm_is_cached(ssm: Stubber) -> None:
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": SONNET})), {"Name": "/fast/model"})
    source = ModelSpecSource(settings(model_spec_parameter="/fast/model"))

    first, second = source.current(), source.current()  # one SSM call within the TTL

    assert first == second == ModelSpec(model_id=SONNET)
    ssm.assert_no_pending_responses()


def test_spec_is_reloaded_after_ttl(ssm: Stubber) -> None:
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": SONNET})), {"Name": "/fast/model"})
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": HAIKU})), {"Name": "/fast/model"})
    source = ModelSpecSource(settings(model_spec_parameter="/fast/model"), ttl_seconds=0)

    assert [source.current().model_id, source.current().model_id] == [SONNET, HAIKU]


@pytest.mark.parametrize(
    "respond",
    [
        lambda stub: stub.add_response("get_parameter", parameter(json.dumps({"model_id": NOVA}))),
        lambda stub: stub.add_response("get_parameter", parameter("{")),
        lambda stub: stub.add_client_error("get_parameter", "ParameterNotFound"),
    ],
)
def test_invalid_or_missing_spec_falls_back_to_default(ssm: Stubber, respond: Callable[[Stubber], None]) -> None:
    respond(ssm)

    assert ModelSpecSource(settings(model_spec_parameter="/fast/model")).current() == ModelSpec(model_id=HAIKU)


def test_bedrock_model_for_claude_sets_effort_per_mode_and_never_temperature() -> None:
    spec = ModelSpec(model_id=HAIKU, max_tokens=4096)

    chat, plan = bedrock_model(spec, Mode.CHAT, "eu-west-1"), bedrock_model(spec, Mode.PLAN, "eu-west-1")

    assert chat.config["additional_request_fields"] == {"output_config": {"effort": "low"}}
    assert plan.config["additional_request_fields"] == {"output_config": {"effort": "medium"}}
    assert chat.config["max_tokens"] == 4096
    assert "temperature" not in chat.config
    assert chat.client.meta.region_name == "eu-west-1"
    assert bedrock_model(spec, Mode.CHAT, "eu-west-1") is chat  # clients are reused


def test_bedrock_model_sends_no_effort_to_other_providers() -> None:
    model = bedrock_model(ModelSpec(model_id=NOVA), Mode.PLAN, "us-east-1")

    assert model.config.get("additional_request_fields") is None
