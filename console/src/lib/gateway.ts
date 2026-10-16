/**
 * AgentCore Gateway MCP client for direct tool calls from the console (Channels and Library views), authorised by
 * the user's access token and the Gateway's Cedar policy. Verified against a live gateway (CUSTOM_JWT inbound):
 * `tools/call` works without `initialize` (no Mcp-Session-Id), answers `application/json`, and CORS allows any
 * origin with `authorization,content-type` (401s carry CORS headers too, so the browser can read them).
 */
import { sseData, textChunks } from "./sse";

/** Tool error codes of the registry (CONTRACTS §B) plus the transport-level ones raised here. */
export type ToolErrorCode =
  | "VALIDATION"
  | "NOT_FOUND"
  | "CONFLICT"
  | "THROTTLED"
  | "PLAN_STALE"
  | "LADDER_MISMATCH"
  | "QUOTA"
  | "FORBIDDEN"
  | "UPSTREAM"
  | "UNAUTHENTICATED";

export class ToolCallError extends Error {
  constructor(
    readonly code: ToolErrorCode,
    message: string,
    readonly retryable = false,
    readonly hint = "",
  ) {
    super(message);
    this.name = "ToolCallError";
  }
}

export interface Gateway {
  /** Calls `<target>___<tool>`; resolves to the tool's output model, rejects with {@link ToolCallError}. */
  call<T>(tool: string, args?: Record<string, unknown>): Promise<T>;
}

interface JsonRpcResponse {
  readonly id?: string | number | null;
  readonly result?: {
    readonly isError?: boolean;
    readonly content?: readonly { readonly type: string; readonly text?: string }[];
    readonly structuredContent?: unknown;
  };
  readonly error?: { readonly code: number; readonly message: string };
}

interface ErrorEnvelope {
  readonly ok: false;
  readonly error: { readonly code: ToolErrorCode; readonly message: string; readonly retryable?: boolean; readonly hint?: string };
}

const isErrorEnvelope = (value: unknown): value is ErrorEnvelope =>
  typeof value === "object" && value !== null && (value as { ok?: unknown }).ok === false;

/** Maps a JSON-RPC `tools/call` response to the tool's output, or throws the matching {@link ToolCallError}. */
export function unwrapToolResult(response: JsonRpcResponse): unknown {
  if (response.error) {
    throw rpcError(response.error);
  }
  const text = response.result?.content?.find((part) => part.type === "text")?.text ?? "";
  if (response.result?.isError) {
    // Gateway-side failures (schema validation, Lambda errors) arrive as text, e.g. "ValidationException - …".
    throw new ToolCallError(text.startsWith("ValidationException") ? "VALIDATION" : "UPSTREAM", text);
  }
  const output = response.result?.structuredContent ?? (text ? (JSON.parse(text) as unknown) : null);
  if (isErrorEnvelope(output)) {
    const { code, message, retryable = false, hint = "" } = output.error;
    throw new ToolCallError(code, message, retryable, hint);
  }
  return output;
}

function rpcError({ code, message }: { code: number; message: string }): ToolCallError {
  if (code === -32001) {
    return new ToolCallError("UNAUTHENTICATED", message);
  }
  if (message.startsWith("Tool Execution Denied")) {
    // Cedar policy decision (agentcore.md §6.2); the Lambda never ran.
    return new ToolCallError("FORBIDDEN", message);
  }
  return new ToolCallError(code === -32602 ? "VALIDATION" : "UPSTREAM", message);
}

/**
 * Gateway client. Every call carries a fresh `correlation_id` (`console-<uuid>`, accepted by every tool) so that a
 * console action can be traced through the tool logs (SPEC §9).
 */
export function gatewayClient(url: string, accessToken: () => Promise<string>): Gateway {
  return {
    async call<T>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
      const id = crypto.randomUUID();
      const response = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${await accessToken()}`,
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
        },
        body: JSON.stringify({
          jsonrpc: "2.0",
          id,
          method: "tools/call",
          params: { name: tool, arguments: { correlation_id: `console-${crypto.randomUUID()}`, ...args } },
        }),
      });
      return unwrapToolResult(await readRpcResponse(response, id)) as T;
    },
  };
}

/** Reads the JSON-RPC response from a plain JSON body or, if the gateway streams, from its SSE body. */
async function readRpcResponse(response: Response, id: string): Promise<JsonRpcResponse> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("text/event-stream") && response.body) {
    for await (const data of sseData(textChunks(response.body))) {
      const message = JSON.parse(data) as JsonRpcResponse;
      if (message.id === id) {
        return message;
      }
    }
    throw new ToolCallError("UPSTREAM", "Gateway stream ended without a response");
  }
  if (contentType.includes("json")) {
    return (await response.json()) as JsonRpcResponse;
  }
  throw new ToolCallError(response.status === 401 ? "UNAUTHENTICATED" : "UPSTREAM", `Gateway returned HTTP ${response.status}`);
}
