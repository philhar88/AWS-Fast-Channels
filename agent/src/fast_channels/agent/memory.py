"""Conversation persistence: AgentCore Memory short-term events, with tool traffic kept out of extraction.

The Strands session manager stores every message as a Memory event, including ``toolUse``/``toolResult``
messages (catalog and CMS text). Long-term strategies would mine those as if the operator had said them, which
turns content metadata into persistent prompt injection (_review.md, Memory). CreateEvent's documented
``extractionMode=SKIP`` keeps such events in short-term memory (history and pending approvals still restore)
while excluding them from long-term extraction. Only operator prompts and assistant replies stay eligible.
"""

import json
import tempfile
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from strands.session import FileSessionManager, SessionManager

if TYPE_CHECKING:
    from fast_channels.agent.settings import Settings

_TOOL_BLOCKS = frozenset({"toolUse", "toolResult"})


def session_manager(settings: Settings, *, session_id: str, actor_id: str) -> SessionManager:
    """Session manager for one (actor, session) conversation.

    Without ``MEMORY_ID`` (local development) sessions persist under the temp directory instead.
    """
    if settings.memory_id is None:
        return FileSessionManager(session_id=session_id, storage_dir=str(_local_sessions_dir()))
    retrieval = (
        {settings.preference_namespace: RetrievalConfig(top_k=5, relevance_score=0.5)}
        if settings.preference_namespace
        else None
    )
    config = AgentCoreMemoryConfig(
        memory_id=settings.memory_id,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config=retrieval,
        async_mode=True,  # offload per-message CreateEvent calls from the event loop serving the stream
    )
    return AgentCoreMemorySessionManager(
        config, region_name=settings.region, boto_session=_memory_session(settings.region)
    )


def skip_extraction_unless_dialogue(params: dict[str, Any], **_: Any) -> None:
    """botocore ``provide-client-params`` handler: mark every CreateEvent that is not dialogue ``SKIP``.

    Dialogue is a conversational payload whose message has no tool blocks. Session/agent-state blobs and tool
    traffic are skipped.
    """
    if not all(_is_dialogue(item) for item in params.get("payload", ())):
        params["extractionMode"] = "SKIP"


def _is_dialogue(item: dict[str, Any]) -> bool:
    conversational = item.get("conversational")
    if conversational is None:
        return False
    try:
        message = json.loads(conversational["content"]["text"])["message"]
    except KeyError, TypeError, ValueError:
        return True  # plain text written by another integration
    return not any(_TOOL_BLOCKS & block.keys() for block in message.get("content", ()))


@cache
def _memory_session(region: str) -> boto3.Session:
    # Handlers registered on a session are inherited by every client it creates, including the ones the session
    # manager builds (and writes the initial session event with) inside its constructor.
    session = boto3.Session(region_name=region)
    session.events.register("provide-client-params.bedrock-agentcore.CreateEvent", skip_extraction_unless_dialogue)
    return session


def _local_sessions_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "fast-channels-agent-sessions"
    path.mkdir(exist_ok=True)
    return path
