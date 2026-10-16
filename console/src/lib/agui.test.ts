import { afterEach, describe, expect, it, vi } from "vitest";
import { agentCoreClient, AgentHttpError, newSessionId, parseEvent, type RunAgentInput } from "./agui";

const input: RunAgentInput = {
  threadId: newSessionId(),
  runId: "run-1",
  messages: [{ id: "u1", role: "user", content: "hi" }],
  state: {},
  tools: [],
  context: [],
  forwardedProps: {},
};

describe("agui", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("generates session ids AgentCore accepts (at least 33 characters)", () => {
    expect(newSessionId().length).toBeGreaterThanOrEqual(33);
  });

  it("parses rendered events and skips the rest", () => {
    expect(parseEvent('{"type":"TEXT_MESSAGE_CONTENT","messageId":"m","delta":"x"}')).toEqual({ type: "TEXT_MESSAGE_CONTENT", messageId: "m", delta: "x" });
    expect(parseEvent('{"type":"REASONING_START","messageId":"m"}')).toBeNull();
  });

  it("posts RunAgentInput with the bearer token and session header, and streams the events", async () => {
    const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(() =>
      Promise.resolve(
        new Response('data: {"type":"RUN_STARTED","threadId":"t","runId":"r"}\n\ndata: {"type":"STEP_STARTED","stepName":"s"}\n\ndata: {"type":"RUN_FINISHED","threadId":"t","runId":"r"}\n\n', {
          headers: { "content-type": "text/event-stream" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const agent = agentCoreClient("https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn/invocations?qualifier=DEFAULT", () => Promise.resolve("access"));
    const types: string[] = [];
    for await (const event of agent.run(input, new AbortController().signal)) {
      types.push(event.type);
    }
    expect(types).toEqual(["RUN_STARTED", "RUN_FINISHED"]);
    const headers = new Headers(fetchMock.mock.calls[0]?.[1].headers);
    expect(headers.get("authorization")).toBe("Bearer access");
    expect(headers.get("x-amzn-bedrock-agentcore-runtime-session-id")).toBe(input.threadId);
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1].body as string)).toEqual(input);
  });

  it("raises HTTP errors with the service message", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(Response.json({ message: "Claim 'client_id' value mismatch with configuration." }, { status: 401 })),
    );
    const run = agentCoreClient("https://x", () => Promise.resolve("id-token")).run(input, new AbortController().signal);
    await expect(run[Symbol.asyncIterator]().next()).rejects.toEqual(new AgentHttpError(401, "Claim 'client_id' value mismatch with configuration."));
  });
});
