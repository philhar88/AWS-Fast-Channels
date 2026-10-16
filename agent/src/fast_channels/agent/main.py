"""AgentCore Runtime entry point (ProtocolType AGUI): ``POST /invocations`` (SSE), ``/ws`` and ``GET /ping``.

``AGUIApp`` from the AgentCore SDK parses the AG-UI ``RunAgentInput`` and encodes the events this handler yields
with the ag-ui-protocol ``EventEncoder``. The runtime session id (``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``)
identifies the conversation; the console sends the same value as ``threadId``.
"""

import logging
from collections.abc import AsyncIterator
from functools import cache

# Runtime imports: AGUIApp inspects the entrypoint signature, which evaluates its annotations.
from ag_ui.core import BaseEvent, RunAgentInput  # noqa: TC002
from bedrock_agentcore.runtime import RequestContext  # noqa: TC002
from bedrock_agentcore.runtime.ag_ui import AGUIApp

from fast_channels.agent.scheduler import Scheduler
from fast_channels.agent.settings import Settings
from fast_channels.agent.telemetry import configure_content_capture

logger = logging.getLogger(__name__)

app = AGUIApp()


@cache
def scheduler() -> Scheduler:
    """The process-wide scheduler, configured from the environment on first use."""
    return Scheduler(Settings.from_env())


@app.entrypoint
async def invocations(run_input: RunAgentInput, context: RequestContext) -> AsyncIterator[BaseEvent]:
    """Run one AG-UI turn."""
    async for event in scheduler().run(run_input, session_id=context.session_id, headers=context.request_headers):
        yield event


def configure_logging() -> None:
    """INFO logs for this package through standard logging.

    Under ``opentelemetry-instrument`` (the Runtime entry point) ADOT exports every record, ``extra=`` fields
    included, to the runtime's ``otel-rt-logs`` stream and prints trace-correlated lines to stdout, so no
    handler is added there. Local runs without ADOT get a plain stderr handler.
    """
    logging.getLogger("fast_channels").setLevel(logging.INFO)
    # Each run rebuilds its agent from the session (fresh system prompt, restored skill state, async hooks), which
    # makes these two SDK modules warn on every request about exactly that.
    for expected_noise in (
        "strands.vended_plugins.skills.agent_skills",
        "bedrock_agentcore.memory.integrations.strands.session_manager",
    ):
        logging.getLogger(expected_noise).setLevel(logging.ERROR)
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")


def serve() -> None:
    """Apply the telemetry content switch (``CAPTURE_PROMPTS``), then serve on port 8080 (AgentCore AGUI contract)."""
    configure_logging()
    logger.info("telemetry content capture", extra={"capture_prompts": configure_content_capture()})
    app.run(port=8080)
