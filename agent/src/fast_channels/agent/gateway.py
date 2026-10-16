"""AgentCore Gateway tools over MCP (streamable HTTP), called with the operator's own token.

The Gateway authorizes every tool call (Cedar) against the caller's JWT, so the agent forwards the
``Authorization`` header it received instead of using its own credentials. One MCP connection serves a whole run
(every tool call of the agent loop) and is closed when the run ends. ``tools/list`` costs a round trip
(~0.4 s measured) and depends only on who is asking, so tool definitions are cached per principal.
"""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from functools import cached_property
from typing import TYPE_CHECKING, Any, Final

from strands.tools.mcp import MCPAgentTool, MCPClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Hashable

    from strands.types.tools import ToolSpec

SEPARATOR: Final = "___"
"""Gateway tool names are ``<target>___<tool>`` (three underscores; agentcore.md §5.1)."""
CORRELATION_FIELD: Final = "correlation_id"
TOOLS_TTL_SECONDS = 300.0


def is_gateway_tool(name: str) -> bool:
    """True for registry tools exposed by a Gateway target."""
    return SEPARATOR in name


class GatewayTool(MCPAgentTool):
    """A Gateway tool whose schema hides ``correlation_id``: the runtime sets it, the model never should."""

    @cached_property
    def tool_spec(self) -> ToolSpec:  # type: ignore[override]
        spec = super().tool_spec
        schema = dict(spec["inputSchema"]["json"])
        schema["properties"] = {k: v for k, v in schema.get("properties", {}).items() if k != CORRELATION_FIELD}
        schema["required"] = [k for k in schema.get("required", []) if k != CORRELATION_FIELD]
        return {**spec, "inputSchema": {"json": schema}}


class ToolCatalog:
    """Gateway tool definitions per principal, kept for a few minutes (Cedar filters ``tools/list`` by caller)."""

    def __init__(self, ttl_seconds: float = TOOLS_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[Hashable, tuple[float, list[Any]]] = {}
        self._lock = threading.Lock()

    def tools(self, client: MCPClient, principal: Hashable) -> list[GatewayTool]:
        """Tools bound to ``client`` (an active connection), listing them only on a cache miss."""
        now = time.monotonic()
        with self._lock:
            self._entries = {k: v for k, v in self._entries.items() if v[0] > now}
            entry = self._entries.get(principal)
        if entry is None:
            definitions = [tool.mcp_tool for tool in _list_all(client) if is_gateway_tool(tool.tool_name)]
            with self._lock:
                self._entries[principal] = (now + self._ttl, definitions)
        else:
            definitions = entry[1]
        return [GatewayTool(definition, client) for definition in definitions]


@asynccontextmanager
async def gateway_tools(
    url: str, authorization: str, *, principal: Hashable, catalog: ToolCatalog
) -> AsyncIterator[list[GatewayTool]]:
    """Open a Gateway connection authorized as the caller and yield its tools; closes it on exit.

    ``MCPClient.start``/``stop`` block on a background thread, so they run off the event loop.
    """
    client = MCPClient(url=url, headers={"Authorization": authorization}, application_name="fast-channels-agent")
    await asyncio.to_thread(client.start)
    try:
        yield await asyncio.to_thread(catalog.tools, client, principal)
    finally:
        await asyncio.to_thread(client.stop, None, None, None)


def _list_all(client: MCPClient) -> list[MCPAgentTool]:
    tools: list[MCPAgentTool] = []
    token: str | None = None
    while True:
        page = client.list_tools_sync(token)
        tools.extend(page)
        if (token := page.pagination_token) is None:
            return tools
