import { afterEach, describe, expect, it, vi } from "vitest";
import { gatewayClient, ToolCallError, unwrapToolResult } from "./gateway";

const text = (value: string) => ({ result: { isError: false, content: [{ type: "text", text: value }] } });

/** Expects `fn` to throw a ToolCallError and returns it. */
function caught(fn: () => unknown): ToolCallError {
  try {
    fn();
  } catch (error) {
    expect(error).toBeInstanceOf(ToolCallError);
    return error as ToolCallError;
  }
  throw new Error("expected a ToolCallError");
}

// Response shapes captured from a live AgentCore Gateway (CUSTOM_JWT inbound, Lambda target), 2026-10-16.
describe("unwrapToolResult", () => {
  it("returns the tool output parsed from the text content", () => {
    expect(unwrapToolResult(text('{"channel":"fast-demo","state":"RUNNING"}'))).toEqual({ channel: "fast-demo", state: "RUNNING" });
  });

  it("prefers structuredContent when present", () => {
    expect(unwrapToolResult({ result: { content: [{ type: "text", text: "{}" }], structuredContent: { a: 1 } } })).toEqual({ a: 1 });
  });

  it("raises the registry error envelope with its code, retryable flag and hint", () => {
    const error = caught(() =>
      unwrapToolResult(text('{"ok":false,"error":{"code":"CONFLICT","message":"channel not running","retryable":false,"hint":"start it first"}}')),
    );
    expect([error.code, error.message, error.retryable, error.hint]).toEqual(["CONFLICT", "channel not running", false, "start it first"]);
  });

  it("maps gateway-side failures", () => {
    const validation = { result: { isError: true, content: [{ type: "text", text: "ValidationException - Missing required field(s): 'channel'" }] } };
    expect(caught(() => unwrapToolResult(validation)).code).toBe("VALIDATION");
    expect(caught(() => unwrapToolResult({ error: { code: -32602, message: "Unknown tool: channels___nope" } })).code).toBe("VALIDATION");
    expect(caught(() => unwrapToolResult({ error: { code: -32001, message: "Invalid Bearer token" } })).code).toBe("UNAUTHENTICATED");
    const denied = { error: { code: -32002, message: "Tool Execution Denied: Tool call not allowed due to policy enforcement" } };
    expect(caught(() => unwrapToolResult(denied)).code).toBe("FORBIDDEN");
  });
});

describe("gatewayClient", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends a stateless JSON-RPC tools/call with the bearer token and a correlation id", async () => {
    const fetchMock = vi.fn((_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string) as { id: string };
      return Promise.resolve(Response.json({ jsonrpc: "2.0", id: body.id, ...text('{"channels":[]}') }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const gateway = gatewayClient("https://gw.example/mcp", () => Promise.resolve("token-1"));

    await expect(gateway.call("channels___list_channels", { limit: 5 })).resolves.toEqual({ channels: [] });
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("https://gw.example/mcp");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer token-1");
    const body = JSON.parse(init?.body as string) as { method: string; params: { name: string; arguments: Record<string, unknown> } };
    expect(body.method).toBe("tools/call");
    expect(body.params.name).toBe("channels___list_channels");
    expect(body.params.arguments).toMatchObject({ limit: 5, correlation_id: expect.stringMatching(/^console-/) as unknown });
  });

  it("reads the matching response from an SSE body", async () => {
    vi.stubGlobal("fetch", (_url: string, init: RequestInit) => {
      const { id } = JSON.parse(init.body as string) as { id: string };
      const events = `data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\ndata: ${JSON.stringify({ id, ...text('{"ok":true}') })}\n\n`;
      return Promise.resolve(new Response(events, { headers: { "content-type": "text/event-stream" } }));
    });
    const gateway = gatewayClient("https://gw.example/mcp", () => Promise.resolve("t"));
    await expect(gateway.call("x___y")).resolves.toEqual({ ok: true });
  });
});
