/** Conversation state of the Assistant view, folded from local actions and AG-UI events. */
import type { AgUiEvent, AgentSnapshot, ApprovalDecision, ApprovalRequest } from "./agui";

export type ToolStatus = "running" | "succeeded" | "failed";

export type TranscriptItem =
  | { readonly kind: "user"; readonly id: string; readonly text: string }
  | { readonly kind: "assistant"; readonly id: string; readonly text: string; readonly streaming: boolean }
  | {
      readonly kind: "tool";
      readonly id: string;
      readonly name: string;
      readonly args: string;
      readonly status: ToolStatus;
      readonly result?: string;
    }
  | {
      readonly kind: "approval";
      readonly id: string;
      readonly request: ApprovalRequest;
      readonly decision?: ApprovalDecision;
    }
  | { readonly kind: "error"; readonly id: string; readonly message: string };

export interface Transcript {
  readonly items: readonly TranscriptItem[];
  readonly running: boolean;
  /** Latest STATE_SNAPSHOT: the plan or schedule the agent is working on. */
  readonly snapshot: AgentSnapshot | null;
}

export type TranscriptAction =
  | { readonly type: "prompt"; readonly id: string; readonly text: string }
  | { readonly type: "decide"; readonly decision: ApprovalDecision }
  | { readonly type: "event"; readonly event: AgUiEvent }
  | { readonly type: "failed"; readonly message: string }
  /** The stream closed; covers a run that ended without RUN_FINISHED. */
  | { readonly type: "settled" }
  | { readonly type: "reset" };

export const EMPTY_TRANSCRIPT: Transcript = { items: [], running: false, snapshot: null };

export function transcriptReducer(state: Transcript, action: TranscriptAction): Transcript {
  switch (action.type) {
    case "reset":
      return EMPTY_TRANSCRIPT;
    case "prompt":
      return { ...append(state, { kind: "user", id: action.id, text: action.text }), running: true };
    case "decide":
      return {
        ...state,
        running: true,
        items: update(state.items, action.decision.interrupt_id, (item) =>
          item.kind === "approval" ? { ...item, decision: action.decision } : item,
        ),
      };
    case "failed":
      return { ...append(state, errorItem(state, action.message)), running: false };
    case "settled":
      return state.running ? { ...state, running: false, items: state.items.map(endStreaming) } : state;
    case "event":
      return applyEvent(state, action.event);
  }
}

function applyEvent(state: Transcript, event: AgUiEvent): Transcript {
  switch (event.type) {
    case "RUN_STARTED":
      return { ...state, running: true };
    case "RUN_FINISHED":
      return { ...state, running: false, items: state.items.map(endStreaming) };
    case "RUN_ERROR": {
      const message = event.code ? `${event.message} (${event.code})` : event.message;
      return { ...state, running: false, items: [...state.items.map(endStreaming), errorItem(state, message)] };
    }
    case "TEXT_MESSAGE_START":
      return append(state, { kind: "assistant", id: event.messageId, text: "", streaming: true });
    case "TEXT_MESSAGE_CONTENT":
      return has(state, event.messageId)
        ? patch(state, event.messageId, (item) => (item.kind === "assistant" ? { ...item, text: item.text + event.delta } : item))
        : append(state, { kind: "assistant", id: event.messageId, text: event.delta, streaming: true });
    case "TEXT_MESSAGE_END":
      return patch(state, event.messageId, endStreaming);
    case "TOOL_CALL_START":
      return append(state, { kind: "tool", id: event.toolCallId, name: event.toolCallName, args: "", status: "running" });
    case "TOOL_CALL_ARGS":
      return patch(state, event.toolCallId, (item) => (item.kind === "tool" ? { ...item, args: item.args + event.delta } : item));
    case "TOOL_CALL_END":
      return state;
    case "TOOL_CALL_RESULT":
      return patch(state, event.toolCallId, (item) =>
        item.kind === "tool" ? { ...item, result: event.content, status: toolStatus(event.content) } : item,
      );
    case "STATE_SNAPSHOT":
      return { ...state, snapshot: event.snapshot };
    case "CUSTOM":
      return event.name === "approval_required" ? approvalRequested(state, event.value as ApprovalRequest) : state;
  }
}

function approvalRequested(state: Transcript, request: ApprovalRequest): Transcript {
  return append(state, { kind: "approval", id: request.interrupt_id, request });
}

/** Tools answer with their output model, or with the `{"ok": false, "error": …}` envelope (SPEC §6). */
function toolStatus(content: string): ToolStatus {
  try {
    const parsed = JSON.parse(content) as { ok?: unknown } | null;
    return parsed?.ok === false ? "failed" : "succeeded";
  } catch {
    return "succeeded";
  }
}

/** The approval card still waiting for the operator, if any. */
export function pendingApproval(transcript: Transcript): Extract<TranscriptItem, { kind: "approval" }> | undefined {
  return transcript.items.find(
    (item): item is Extract<TranscriptItem, { kind: "approval" }> => item.kind === "approval" && !item.decision,
  );
}

const endStreaming = (item: TranscriptItem): TranscriptItem =>
  item.kind === "assistant" && item.streaming ? { ...item, streaming: false } : item;

const errorItem = (state: Transcript, message: string): TranscriptItem => ({
  kind: "error",
  id: `error-${state.items.length}`,
  message,
});

const has = (state: Transcript, id: string): boolean => state.items.some((item) => item.id === id);

const append = (state: Transcript, item: TranscriptItem): Transcript => ({ ...state, items: [...state.items, item] });

const patch = (state: Transcript, id: string, fn: (item: TranscriptItem) => TranscriptItem): Transcript => ({
  ...state,
  items: update(state.items, id, fn),
});

const update = (
  items: readonly TranscriptItem[],
  id: string,
  fn: (item: TranscriptItem) => TranscriptItem,
): readonly TranscriptItem[] => items.map((item) => (item.id === id ? fn(item) : item));
