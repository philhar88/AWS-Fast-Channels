import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { AgentHttpError, newSessionId, type AgentClient, type AgUiMessage, type ApprovalDecision, type RunAgentInput } from "./agui";
import { errorMessage } from "./errors";
import { EMPTY_TRANSCRIPT, transcriptReducer, type Transcript } from "./transcript";

export interface AgentChat {
  readonly transcript: Transcript;
  /** AgentCore runtime session id = AG-UI threadId; one per conversation. */
  readonly sessionId: string;
  /** A run's stream is open: until it closes, `send` and `decide` are ignored. */
  readonly busy: boolean;
  /** Sends a message; ignored while a run is in flight. */
  readonly send: (text: string) => void;
  /** Answers one approval; ignored while a run is in flight. */
  readonly decide: (decision: ApprovalDecision) => void;
  /** Starts a new conversation (and a new runtime session), abandoning a run in flight. */
  readonly reset: () => void;
}

/**
 * Drives AG-UI runs against `agent` and folds their events into a {@link Transcript}.
 *
 * A session runs one turn at a time. Aborting a stream stops only the client: a second run would race the first
 * one on the agent's session state (and could ask again for a call already executing), so `send` and `decide` do
 * nothing until the current run's stream has closed (`busy`).
 */
export function useAgentChat(agent: AgentClient): AgentChat {
  const [transcript, dispatch] = useReducer(transcriptReducer, EMPTY_TRANSCRIPT);
  const [sessionId, setSessionId] = useState(newSessionId);
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);

  const run = useCallback(
    async (messages: AgUiMessage[], forwardedProps: RunAgentInput["forwardedProps"]) => {
      const controller = new AbortController();
      active.current = controller;
      setBusy(true);
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
          if (controller.signal.aborted) {
            return; // abandoned by reset(): its events must not reach the new conversation
          }
          dispatch({ type: "event", event });
        }
        dispatch({ type: "settled" });
      } catch (error) {
        if (!controller.signal.aborted) {
          dispatch({ type: "failed", message: describe(error) });
        }
      } finally {
        if (active.current === controller) {
          active.current = null;
          setBusy(false);
        }
      }
    },
    [agent, sessionId, transcript.snapshot],
  );

  useEffect(() => () => active.current?.abort(), []);

  return {
    transcript,
    sessionId,
    busy,
    send: (text) => {
      if (active.current) {
        return;
      }
      const id = crypto.randomUUID();
      dispatch({ type: "prompt", id, text });
      void run([{ id, role: "user", content: text }], {});
    },
    decide: (decision) => {
      if (active.current) {
        return;
      }
      dispatch({ type: "decide", decision });
      void run([], { approval: decision });
    },
    reset: () => {
      active.current?.abort();
      active.current = null;
      setBusy(false);
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
