import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

import fast_channels.agent.model as model_module
from fast_channels.agent.model import (
    ActiveSpec,
    Effort,
    ModelSpec,
    ModelSpecSource,
    SpecSource,
    TaskEntry,
    bedrock_model,
    parse_spec,
    parse_task_entry,
)
from fast_channels.agent.protocol import Mode
from fast_channels.agent.settings import Settings

HAIKU = "us.anthropic.claude-haiku-5-5"
SONNET = "us.anthropic.claude-sonnet-5-5"
NOVA = "us.amazon.nova-2-lite-v1:0"
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "model-registry.schema.json"
TASK_PARAMETER = "/fast/models/tasks/scheduler.plan"


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
    assert ModelSpecSource(settings()).current() == ActiveSpec(ModelSpec(model_id=HAIKU), SpecSource.DEFAULT)


@pytest.fixture
def ssm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stubber]:
    client = boto3.client("ssm", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(model_module, "_ssm", lambda region: client)
    with Stubber(client) as stubber:
        yield stubber


def parameter(value: str, name: str = "/fast/model") -> dict[str, Any]:
    return {"Parameter": {"Name": name, "Type": "String", "Value": value}}


def test_spec_from_ssm_is_cached(ssm: Stubber) -> None:
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": SONNET})), {"Name": "/fast/model"})
    source = ModelSpecSource(settings(model_spec_parameter="/fast/model"))

    first, second = source.current(), source.current()  # one SSM call within the TTL

    assert first == second == ActiveSpec(ModelSpec(model_id=SONNET), SpecSource.MODEL_SPEC)
    ssm.assert_no_pending_responses()


def test_spec_is_reloaded_after_ttl(ssm: Stubber) -> None:
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": SONNET})), {"Name": "/fast/model"})
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": HAIKU})), {"Name": "/fast/model"})
    source = ModelSpecSource(settings(model_spec_parameter="/fast/model"), ttl_seconds=0)

    assert [source.current().spec.model_id, source.current().spec.model_id] == [SONNET, HAIKU]


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

    active = ModelSpecSource(settings(model_spec_parameter="/fast/model")).current()

    assert active == ActiveSpec(ModelSpec(model_id=HAIKU), SpecSource.DEFAULT)


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


ENTRY: dict[str, Any] = {
    "model": SONNET,
    "route": "bedrock",
    "effort": {"chat": "low", "plan": "high"},
    "thinking": "adaptive",
    "max_tokens": 8000,
    "data_class": "operational",
    "daily_cap_usd": None,
    "eval": None,
}


def test_registry_entry_becomes_the_spec() -> None:
    spec = parse_task_entry(json.dumps(ENTRY), allowed=frozenset({HAIKU, SONNET}))

    assert spec == ModelSpec(model_id=SONNET, effort=Effort(chat="low", plan="high"), max_tokens=8000)


def test_one_effort_level_applies_to_both_modes() -> None:
    spec = parse_task_entry(
        json.dumps({"model": HAIKU, "effort": "low", "thinking": "disabled"}), allowed=frozenset({HAIKU})
    )

    assert spec == ModelSpec(model_id=HAIKU, effort=Effort(chat="low", plan="low"), thinking="disabled")


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({**ENTRY, "model": NOVA}),  # outside the allow-list
        json.dumps({**ENTRY, "route": "gateway"}),  # the gateway route is not built
        json.dumps({**ENTRY, "base_url": "https://evil.example"}),
        json.dumps({**ENTRY, "provider": "openai"}),
        json.dumps({**ENTRY, "thinking": "enabled"}),
        json.dumps({**ENTRY, "max_tokens": 0}),
        json.dumps({**ENTRY, "effort": "extreme"}),
        json.dumps({**ENTRY, "daily_cap_usd": 0}),
        json.dumps({key: value for key, value in ENTRY.items() if key != "model"}),
    ],
)
def test_invalid_registry_entries_are_refused(raw: str) -> None:
    with pytest.raises(ValueError, match=r"registry entry|not in ALLOWED_MODELS"):
        parse_task_entry(raw, allowed=frozenset({HAIKU, SONNET}))


def test_registry_entry_comes_first(ssm: Stubber) -> None:
    ssm.add_response("get_parameter", parameter(json.dumps(ENTRY), TASK_PARAMETER), {"Name": TASK_PARAMETER})
    source = ModelSpecSource(settings(model_task_parameter=TASK_PARAMETER, model_spec_parameter="/fast/model"))

    active = source.current()

    assert (active.source, active.spec.model_id, active.spec.effort.plan) == (SpecSource.REGISTRY, SONNET, "high")
    ssm.assert_no_pending_responses()  # the ModelSpec is not read


@pytest.mark.parametrize(
    "respond",
    [
        lambda stub: stub.add_response("get_parameter", parameter(json.dumps({**ENTRY, "model": NOVA}))),
        lambda stub: stub.add_response("get_parameter", parameter("{")),
        lambda stub: stub.add_client_error("get_parameter", "ParameterNotFound"),
        lambda stub: stub.add_client_error("get_parameter", "AccessDeniedException"),
    ],
)
def test_unusable_registry_entry_falls_back_to_the_model_spec(
    ssm: Stubber, respond: Callable[[Stubber], None], caplog: pytest.LogCaptureFixture
) -> None:
    respond(ssm)
    ssm.add_response("get_parameter", parameter(json.dumps({"model_id": HAIKU, "max_tokens": 4000})))
    source = ModelSpecSource(settings(model_task_parameter=TASK_PARAMETER, model_spec_parameter="/fast/model"))

    assert source.current() == ActiveSpec(ModelSpec(model_id=HAIKU, max_tokens=4000), SpecSource.MODEL_SPEC)
    [warning] = [r for r in caplog.records if r.levelname == "WARNING"]
    assert (warning.source, warning.parameter) == ("registry", TASK_PARAMETER)  # type: ignore[attr-defined]


def test_without_usable_sources_the_default_applies(ssm: Stubber) -> None:
    ssm.add_client_error("get_parameter", "ParameterNotFound")
    ssm.add_client_error("get_parameter", "ParameterNotFound")
    source = ModelSpecSource(settings(model_task_parameter=TASK_PARAMETER, model_spec_parameter="/fast/model"))

    assert source.current() == ActiveSpec(ModelSpec(model_id=HAIKU), SpecSource.DEFAULT)


def test_registry_entry_without_a_model_spec_parameter(ssm: Stubber) -> None:
    ssm.add_client_error("get_parameter", "ParameterNotFound")
    source = ModelSpecSource(settings(model_task_parameter=TASK_PARAMETER))

    assert source.current().source is SpecSource.DEFAULT
    ssm.assert_no_pending_responses()


def test_bedrock_model_disables_thinking_only_when_the_spec_does() -> None:
    adaptive = bedrock_model(ModelSpec(model_id=HAIKU), Mode.CHAT, "us-east-1")
    disabled = bedrock_model(ModelSpec(model_id=HAIKU, thinking="disabled"), Mode.PLAN, "us-east-1")
    nova = bedrock_model(ModelSpec(model_id=NOVA, thinking="disabled"), Mode.PLAN, "us-east-1")

    assert adaptive.config["additional_request_fields"] == {"output_config": {"effort": "low"}}  # the v3.0.0 request
    assert disabled.config["additional_request_fields"] == {
        "output_config": {"effort": "medium"},
        "thinking": {"type": "disabled"},
    }
    assert nova.config.get("additional_request_fields") is None


def _structure(schema: Any, definitions: dict[str, Any]) -> Any:
    """``schema`` with every ``$ref`` inlined and its documentation (titles, descriptions) removed: what an entry
    must look like, whatever the models that produce the schema are called."""
    if isinstance(schema, list):
        return [_structure(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        inlined = definitions[schema["$ref"].rsplit("/", 1)[-1]] | {k: v for k, v in schema.items() if k != "$ref"}
        return _structure(inlined, definitions)
    if "const" in schema:  # pydantic writes a one-value Literal as const, a one-member enum as enum
        schema = {"enum": [schema["const"]]} | {k: v for k, v in schema.items() if k != "const"}
    skipped = {"$defs", "$schema", "title", "description", "examples"}
    return {
        key: {name: _structure(field, definitions) for name, field in value.items()}
        if key == "properties"
        else _structure(value, definitions)
        for key, value in schema.items()
        if key not in skipped
    }


def test_the_agent_reads_the_registry_contract() -> None:
    contract = json.loads(CONTRACT.read_text())
    agent = TaskEntry.model_json_schema()

    assert _structure(agent, agent.get("$defs", {})) == _structure(contract, contract["$defs"])
    for example in contract["examples"]:
        assert TaskEntry.model_validate(example).spec().model_id == example["model"]
