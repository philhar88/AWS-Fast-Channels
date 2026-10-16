import asyncio
import time
from collections.abc import Sequence
from typing import Any

import pytest
from mcp.types import Tool
from strands.types.collections import PaginatedList

import fast_channels.agent.gateway as gateway_module
from fast_channels.agent.gateway import GatewayTool, ToolCatalog, gateway_tools, is_gateway_tool
from fast_channels.agent.protocol import AgentRunError


def mcp_tool(name: str, properties: dict[str, Any] | None = None, required: Sequence[str] = ()) -> Tool:
    schema = {"type": "object", "properties": properties or {}, "required": list(required)}
    return Tool(name=name, description=f"{name} tool.", input_schema=schema)


class FakeClient:
    """MCPClient stand-in: paginated tools/list that counts calls."""

    def __init__(self, *pages: list[Tool]) -> None:
        self.pages = pages
        self.list_calls = 0
        self.started = self.stopped = False

    def list_tools_sync(self, pagination_token: str | None = None) -> PaginatedList[GatewayTool]:
        self.list_calls += 1
        index = int(pagination_token or 0)
        token = str(index + 1) if index + 1 < len(self.pages) else None
        return PaginatedList([GatewayTool(t, self) for t in self.pages[index]], token=token)  # type: ignore[arg-type]

    def start(self) -> FakeClient:
        self.started = True
        return self

    def stop(self, *_: Any) -> None:
        self.stopped = True


def test_correlation_id_is_hidden_from_the_model() -> None:
    properties = {"channel": {"type": "string"}, "correlation_id": {"type": "string"}}
    tool = GatewayTool(mcp_tool("schedule___plan", properties, ["channel", "correlation_id"]), FakeClient())  # type: ignore[arg-type]

    schema = tool.tool_spec["inputSchema"]["json"]

    assert schema["properties"] == {"channel": {"type": "string"}}
    assert schema["required"] == ["channel"]
    assert tool.tool_name == "schedule___plan"


def test_catalog_lists_all_pages_once_per_principal_and_keeps_only_registry_tools() -> None:
    first = FakeClient(
        [mcp_tool("x_amz_bedrock_agentcore_search"), mcp_tool("catalog___get_content")], [mcp_tool("schedule___plan")]
    )
    catalog = ToolCatalog()
    principal = ("operator", "console", ())

    tools = catalog.tools(first, principal)  # type: ignore[arg-type]
    second = FakeClient()
    cached = catalog.tools(second, principal)  # type: ignore[arg-type]

    assert [t.tool_name for t in tools] == ["catalog___get_content", "schedule___plan"]
    assert first.list_calls == 2
    assert [t.tool_name for t in cached] == ["catalog___get_content", "schedule___plan"]
    assert second.list_calls == 0
    assert {id(t.mcp_client) for t in cached} == {id(second)}  # definitions are cached, connections are not


class WarmingClient(FakeClient):
    """A Gateway that lists no tools for the first ``empty`` calls (seen right after a deployment)."""

    def __init__(self, empty: int, tools: list[Tool]) -> None:
        super().__init__(tools)
        self.empty = empty

    def list_tools_sync(self, pagination_token: str | None = None) -> PaginatedList[GatewayTool]:
        if self.list_calls < self.empty:
            self.list_calls += 1
            return PaginatedList([], token=None)
        return super().list_tools_sync(pagination_token)


def test_an_empty_tool_list_is_retried_once_and_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    pauses: list[float] = []
    monkeypatch.setattr(time, "sleep", pauses.append)
    catalog = ToolCatalog(retry_seconds=1.0)
    client = WarmingClient(1, [mcp_tool("schedule___plan")])

    tools = catalog.tools(client, "p")  # type: ignore[arg-type]
    catalog.tools(client, "p")  # type: ignore[arg-type]

    assert [t.tool_name for t in tools] == ["schedule___plan"]
    assert (client.list_calls, pauses) == (2, [1.0])  # the non-empty list is cached


def test_a_gateway_without_tools_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    catalog = ToolCatalog()
    client = WarmingClient(3, [mcp_tool("schedule___plan")])

    with pytest.raises(AgentRunError) as raised:
        catalog.tools(client, "p")  # type: ignore[arg-type]

    assert raised.value.code == "GATEWAY_UNAVAILABLE"
    assert client.list_calls == 2
    assert [t.tool_name for t in catalog.tools(client, "p")] == ["schedule___plan"]  # type: ignore[arg-type]


def test_catalog_entries_expire() -> None:
    client = FakeClient([mcp_tool("schedule___plan")])
    catalog = ToolCatalog(ttl_seconds=0)

    catalog.tools(client, "a")  # type: ignore[arg-type]
    catalog.tools(client, "a")  # type: ignore[arg-type]

    assert client.list_calls == 2


def test_gateway_tools_forwards_the_caller_token_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, Any] = {}

    def fake_mcp_client(**kwargs: Any) -> FakeClient:
        created.update(kwargs, client=FakeClient([mcp_tool("schedule___plan")]))
        return created["client"]  # type: ignore[no-any-return]

    monkeypatch.setattr(gateway_module, "MCPClient", fake_mcp_client)

    async def use() -> tuple[list[str], bool]:
        async with gateway_tools("https://gw/mcp", "Bearer abc", principal="p", catalog=ToolCatalog()) as tools:
            return [t.tool_name for t in tools], created["client"].stopped

    names, stopped_inside = asyncio.run(use())

    assert created["url"] == "https://gw/mcp"
    assert created["headers"] == {"Authorization": "Bearer abc"}
    assert names == ["schedule___plan"]
    assert stopped_inside is False
    assert created["client"].started and created["client"].stopped


def test_is_gateway_tool() -> None:
    assert is_gateway_tool("schedule___publish")
    assert not is_gateway_tool("skills")
