import { describe, expect, it } from "vitest";
import type { AgUiEvent } from "./agui";
import { EMPTY_TRANSCRIPT, pendingApproval, transcriptReducer, type Transcript } from "./transcript";

const fold = (state: Transcript, ...events: AgUiEvent[]): Transcript =>
  events.reduce((s, event) => transcriptReducer(s, { type: "event", event }), state);

const run = { threadId: "t", runId: "r" };

describe("transcriptReducer", () => {
  it("folds a run with text, a failed tool call, a snapshot and an approval request", () => {
    const prompted = transcriptReducer(EMPTY_TRANSCRIPT, { type: "prompt", id: "u1", text: "plan" });
    const state = fold(
      prompted,
      { type: "RUN_STARTED", ...run },
      { type: "TEXT_MESSAGE_START", messageId: "m1", role: "assistant" },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "Hello " },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "there" },
      { type: "TEXT_MESSAGE_END", messageId: "m1" },
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "schedule___plan" },
      { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '{"channel":' },
      { type: "TOOL_CALL_ARGS", toolCallId: "c1", delta: '"fast-demo"}' },
      { type: "TOOL_CALL_END", toolCallId: "c1" },
      { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: '{"ok":false,"error":{"code":"PLAN_STALE"}}' },
      { type: "STATE_SNAPSHOT", snapshot: { channel: "fast-demo", plan: null, schedule: null } },
      { type: "CUSTOM", name: "approval_required", value: { interrupt_id: "i1", tool: "schedule___publish", arguments: {}, reason: "why" } },
      { type: "CUSTOM", name: "something_else", value: 1 },
      { type: "RUN_FINISHED", ...run },
    );
    expect(state.running).toBe(false);
    expect(state.snapshot?.channel).toBe("fast-demo");
    expect(state.items).toEqual([
      { kind: "user", id: "u1", text: "plan" },
      { kind: "assistant", id: "m1", text: "Hello there", streaming: false },
      { kind: "tool", id: "c1", name: "schedule___plan", args: '{"channel":"fast-demo"}', status: "failed", result: '{"ok":false,"error":{"code":"PLAN_STALE"}}' },
      { kind: "approval", id: "i1", request: { interrupt_id: "i1", tool: "schedule___publish", arguments: {}, reason: "why" } },
    ]);
    expect(pendingApproval(state)?.id).toBe("i1");

    const decided = transcriptReducer(state, { type: "decide", decision: { interrupt_id: "i1", approved: true, note: null } });
    expect(decided.running).toBe(true);
    expect(pendingApproval(decided)).toBeUndefined();
  });

  it("marks a successful tool result and accepts content without a start event", () => {
    const state = fold(
      EMPTY_TRANSCRIPT,
      { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "channels___list_channels" },
      { type: "TOOL_CALL_RESULT", toolCallId: "c1", content: '{"channels":[]}' },
      { type: "TEXT_MESSAGE_CONTENT", messageId: "m9", delta: "orphan" },
    );
    expect(state.items.map((i) => (i.kind === "tool" ? i.status : i.kind))).toEqual(["succeeded", "assistant"]);
  });

  it("ends streaming and records RUN_ERROR, and settles a stream that closed without RUN_FINISHED", () => {
    const errored = fold(
      EMPTY_TRANSCRIPT,
      { type: "RUN_STARTED", ...run },
      { type: "TEXT_MESSAGE_START", messageId: "m1" },
      { type: "RUN_ERROR", message: "Model throttled", code: "THROTTLED" },
    );
    expect(errored.running).toBe(false);
    expect(errored.items).toEqual([
      { kind: "assistant", id: "m1", text: "", streaming: false },
      { kind: "error", id: "error-1", message: "Model throttled (THROTTLED)" },
    ]);
    const open = fold(EMPTY_TRANSCRIPT, { type: "RUN_STARTED", ...run }, { type: "TEXT_MESSAGE_START", messageId: "m1" });
    const settled = transcriptReducer(open, { type: "settled" });
    expect(settled.running).toBe(false);
    expect(settled.items[0]).toMatchObject({ streaming: false });
  });
});
