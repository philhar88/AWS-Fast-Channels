import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { AgentHttpError, newSessionId, type AgentClient, type AgUiMessage, type ApprovalDecision, type RunAgentInput } from "./agui";
import { errorMessage } from "./errors";
import { EMPTY_TRANSCRIPT, transcriptReducer, type Transcript } from "./transcript";

export interface AgentChat {
  readonly transcript: Transcript;
  /** AgentCore runtime session id = AG-UI threadId; one per conversation. */
  readonly sessionId: string;
  readonly send: (text: string) => void;
  readonly decide: (decision: ApprovalDecision) => void;
  /** Starts a new conversation (and a new runtime session). */
  readonly reset: () => void;
}

/** Drives AG-UI runs against `agent` and folds their events into a {@link Transcript}. */
export function useAgentChat(agent: AgentClient): AgentChat {
  const [transcript, dispatch] = useReducer(transcriptReducer, EMPTY_TRANSCRIPT);
  const [sessionId, setSessionId] = useState(newSessionId);
  const active = useRef<AbortController | null>(null);

  const run = useCallback(
    async (messages: AgUiMessage[], forwardedProps: RunAgentInput["forwardedProps"]) => {
      active.current?.abort();
      const controller = new AbortController();
      active.current = controller;
      const input: RunAgentInput = {
        threadId: sessionId,
        runId: crypto.randomUUID(),
        messages,
        state: transcript.snapshot ?? {},
        tools: [],
        context: [],
        forwardedProps,
      };
      try {
        for await (const event of agent.run(input, controller.signal)) {
          dispatch({ type: "event", event });
        }
        dispatch({ type: "settled" });
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ type: "failed", message: describe(error) });
        }
      }
    },
    [agent, sessionId, transcript.snapshot],
  );

  useEffect(() => () => active.current?.abort(), []);

  return {
    transcript,
    sessionId,
    send: (text) => {
      const id = crypto.randomUUID();
      dispatch({ type: "prompt", id, text });
      void run([{ id, role: "user", content: text }], {});
    },
    decide: (decision) => {
      dispatch({ type: "decide", decision });
      void run([], { approval: decision });
    },
    reset: () => {
      active.current?.abort();
      dispatch({ type: "reset" });
      setSessionId(newSessionId());
    },
  };
}

function describe(error: unknown): string {
  if (error instanceof AgentHttpError && (error.status === 401 || error.status === 403)) {
    return `The agent rejected your credentials (HTTP ${error.status}: ${error.message}). Sign out and in again.`;
  }
  return errorMessage(error);
}
