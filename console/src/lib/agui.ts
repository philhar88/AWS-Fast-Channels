/**
 * AG-UI over AgentCore Runtime (CONTRACTS §G): the request body is an AG-UI `RunAgentInput`, the response an SSE
 * stream of AG-UI events. Only the events the console renders are typed here; others (REASONING_*, STEP_*, RAW…)
 * are skipped. `@ag-ui/client` would do the same job, but its HttpAgent pulls rxjs, zod and protobuf (~470 KB
 * minified) into the bundle to parse one SSE stream.
 */
import type { Epg, Plan } from "./schedule";
import { sseData, textChunks } from "./sse";

export interface AgUiMessage {
  readonly id: string;
  readonly role: "user" | "assistant";
  readonly content: string;
}

/** Operator decision on an `approval_required` interrupt, sent back as `forwardedProps.approval`. */
export interface ApprovalDecision {
  readonly interrupt_id: string;
  readonly approved: boolean;
  readonly note: string | null;
}

/**
 * The agent keeps the conversation in AgentCore Memory and reads only the newest user message, so a prompt run
 * carries just that message and an approval resume carries none. `state` echoes the last STATE_SNAPSHOT (AG-UI
 * shared state).
 */
export interface RunAgentInput {
  readonly threadId: string;
  readonly runId: string;
  readonly messages: readonly AgUiMessage[];
  readonly state: AgentSnapshot | Record<string, never>;
  readonly tools: readonly [];
  readonly context: readonly [];
  readonly forwardedProps: { readonly approval?: ApprovalDecision };
}

/** CUSTOM `approval_required` payload, raised by the agent before a CONFIRM tool runs. */
export interface ApprovalRequest {
  readonly interrupt_id: string;
  readonly tool: string;
  readonly arguments: Readonly<Record<string, unknown>>;
  readonly reason: string;
  readonly tool_call_id?: string;
  readonly cost_estimate_usd?: number;
}

/** STATE_SNAPSHOT payload, emitted after plan, publish and get_schedule. */
export interface AgentSnapshot {
  readonly channel: string;
  readonly plan: Plan | null;
  readonly schedule: Epg | null;
}

export type AgUiEvent =
  | { readonly type: "RUN_STARTED"; readonly threadId: string; readonly runId: string }
  | { readonly type: "RUN_FINISHED"; readonly threadId: string; readonly runId: string }
  | { readonly type: "RUN_ERROR"; readonly message: string; readonly code?: string }
  | { readonly type: "TEXT_MESSAGE_START"; readonly messageId: string; readonly role?: string }
  | { readonly type: "TEXT_MESSAGE_CONTENT"; readonly messageId: string; readonly delta: string }
  | { readonly type: "TEXT_MESSAGE_END"; readonly messageId: string }
  | { readonly type: "TOOL_CALL_START"; readonly toolCallId: string; readonly toolCallName: string }
  | { readonly type: "TOOL_CALL_ARGS"; readonly toolCallId: string; readonly delta: string }
  | { readonly type: "TOOL_CALL_END"; readonly toolCallId: string }
  | { readonly type: "TOOL_CALL_RESULT"; readonly toolCallId: string; readonly content: string }
  | { readonly type: "STATE_SNAPSHOT"; readonly snapshot: AgentSnapshot }
  | { readonly type: "CUSTOM"; readonly name: string; readonly value: unknown };

const RENDERED_EVENTS: ReadonlySet<string> = new Set<AgUiEvent["type"]>([
  "RUN_STARTED",
  "RUN_FINISHED",
  "RUN_ERROR",
  "TEXT_MESSAGE_START",
  "TEXT_MESSAGE_CONTENT",
  "TEXT_MESSAGE_END",
  "TOOL_CALL_START",
  "TOOL_CALL_ARGS",
  "TOOL_CALL_END",
  "TOOL_CALL_RESULT",
  "STATE_SNAPSHOT",
  "CUSTOM",
]);

/** Parses one SSE `data` payload; `null` for events the console does not render. */
export function parseEvent(data: string): AgUiEvent | null {
  const event = JSON.parse(data) as { type?: unknown };
  return typeof event.type === "string" && RENDERED_EVENTS.has(event.type) ? (event as AgUiEvent) : null;
}

/** Anything that turns a `RunAgentInput` into AG-UI events: the AgentCore runtime, or the mock agent. */
export interface AgentClient {
  run(input: RunAgentInput, signal: AbortSignal): AsyncIterable<AgUiEvent>;
}

export class AgentHttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "AgentHttpError";
  }
}

/** One conversation = one AgentCore runtime session; the id doubles as the AG-UI threadId. */
export const newSessionId = (): string => `fast-console-${crypto.randomUUID()}`; // ≥ 33 chars, as AgentCore requires

/**
 * Calls the runtime directly from the browser with the user's Cognito access token (models-ui.md §5.3: CORS is
 * `*`, the ID token is rejected, and the session id header must be at least 33 characters).
 */
export function agentCoreClient(invokeUrl: string, accessToken: () => Promise<string>): AgentClient {
  return {
    async *run(input, signal) {
      const response = await fetch(invokeUrl, {
        method: "POST",
        signal,
        headers: {
          Authorization: `Bearer ${await accessToken()}`,
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": input.threadId,
        },
        body: JSON.stringify(input),
      });
      if (!response.ok || response.body === null) {
        throw new AgentHttpError(response.status, await errorMessage(response));
      }
      for await (const data of sseData(textChunks(response.body))) {
        const event = parseEvent(data);
        if (event !== null) {
          yield event;
        }
      }
    },
  };
}

async function errorMessage(response: Response): Promise<string> {
  const body = await response.text();
  try {
    const { message } = JSON.parse(body) as { message?: string };
    return message ?? body;
  } catch {
    return body || `HTTP ${response.status}`;
  }
}
