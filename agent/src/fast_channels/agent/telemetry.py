"""What the Runtime's telemetry keeps of a run: metadata by default, conversation content only on request.

Under ``opentelemetry-instrument`` ADOT defaults ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`` to ``true``.
The botocore Bedrock instrumentation then logs the system prompt, every operator message, tool result and model
reply to the runtime's ``otel-rt-logs`` stream, and Strands records the same content on its spans. Anyone who can
read the log group would read every operator's conversation, although Memory keeps them apart per actor.

By default both are switched off before the first agent is built: the Bedrock log events keep their tool call ids
and names, and the Strands spans keep their structure, token counts, ``session.id`` and ``actor.id`` with the content
replaced by ``[REDACTED]``. The agent's own records (``run started`` with correlation id, actor, mode and model id)
never carry content. Set ``CAPTURE_PROMPTS=true`` on the Runtime to keep full content, for debugging or evaluations
that need it.
"""

import os
from collections.abc import MutableMapping
from typing import Final

CAPTURE_PROMPTS: Final = "CAPTURE_PROMPTS"
"""Debug switch: ``true`` keeps prompts, tool results and replies in logs and traces."""
BEDROCK_CONTENT: Final = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
"""Read by the botocore Bedrock instrumentation on every model call."""
SEMCONV_OPT_IN: Final = "OTEL_SEMCONV_STABILITY_OPT_IN"
"""Comma-separated tokens; Strands reads them when it creates its tracer (with the first agent)."""
REDACT_ALL: Final = "gen_ai_unredacted_attributes="
"""Strands token: redact message and tool content except the attributes listed after ``=`` (none)."""


def configure_content_capture(env: MutableMapping[str, str] = os.environ) -> bool:
    """Apply the ``CAPTURE_PROMPTS`` switch to ``env``; returns whether content is captured.

    Call it before the first Bedrock request and agent. An unredacted-attributes list that is already set is kept.
    """
    capture = env.get(CAPTURE_PROMPTS, "").strip().lower() == "true"
    env[BEDROCK_CONTENT] = "true" if capture else "false"
    tokens = [token.strip() for token in env.get(SEMCONV_OPT_IN, "").split(",") if token.strip()]
    if not capture and not any(token.startswith(REDACT_ALL) for token in tokens):
        env[SEMCONV_OPT_IN] = ",".join([*tokens, REDACT_ALL])
    return capture
