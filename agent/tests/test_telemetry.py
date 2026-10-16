import pytest
from opentelemetry.instrumentation.botocore.extensions.bedrock_utils import genai_capture_message_content
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.telemetry import tracer as strands_tracer

from fast_channels.agent.telemetry import BEDROCK_CONTENT, CAPTURE_PROMPTS, SEMCONV_OPT_IN, configure_content_capture


def test_content_is_kept_out_of_logs_and_traces_by_default() -> None:
    # ADOT's own default when agent observability is on.
    env = {BEDROCK_CONTENT: "true", SEMCONV_OPT_IN: "gen_ai_latest_experimental"}

    assert configure_content_capture(env) is False
    assert env[BEDROCK_CONTENT] == "false"
    assert env[SEMCONV_OPT_IN] == "gen_ai_latest_experimental,gen_ai_unredacted_attributes="


@pytest.mark.parametrize("value", ["true", " TRUE "])
def test_capture_prompts_keeps_full_content(value: str) -> None:
    env = {CAPTURE_PROMPTS: value}

    assert configure_content_capture(env) is True
    assert env == {CAPTURE_PROMPTS: value, BEDROCK_CONTENT: "true"}


def test_an_explicit_unredacted_list_is_respected() -> None:
    env = {SEMCONV_OPT_IN: "gen_ai_unredacted_attributes=gen_ai.tool.call.arguments"}

    configure_content_capture(env)

    assert env[SEMCONV_OPT_IN] == "gen_ai_unredacted_attributes=gen_ai.tool.call.arguments"
    assert env[BEDROCK_CONTENT] == "false"


def traced_prompt(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    """The serialised user message on a Strands model span, recorded by a tracer created under ``env``."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(strands_tracer.trace_api, "get_tracer_provider", lambda: provider)

    span = strands_tracer.Tracer().start_model_invoke_span([{"role": "user", "content": [{"text": "secret plan"}]}])
    span.end()
    (event,) = (e for s in exporter.get_finished_spans() for e in s.events if e.name == "gen_ai.user.message")
    return str((event.attributes or {})["content"])


def test_strands_spans_and_bedrock_log_events_follow_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    redacted: dict[str, str] = {BEDROCK_CONTENT: "true"}
    configure_content_capture(redacted)
    assert traced_prompt(monkeypatch, redacted) == "[REDACTED]"
    assert genai_capture_message_content() is False

    captured: dict[str, str] = {CAPTURE_PROMPTS: "true", SEMCONV_OPT_IN: ""}
    configure_content_capture(captured)
    assert "secret plan" in traced_prompt(monkeypatch, captured)
    assert genai_capture_message_content() is True
