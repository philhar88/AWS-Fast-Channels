/**
 * A scripted agent speaking the same AG-UI events as the runtime: tool activity, STATE_SNAPSHOTs, and
 * `approval_required` interrupts before CONFIRM tools. Like the runtime it streams the paused tool call first,
 * answers it on resume (a declined call gets a DECLINED envelope), and asks again for approvals a resume left open.
 */
import type { AgentClient, AgentSnapshot, AgUiEvent, ApprovalDecision, ApprovalRequest, RunAgentInput } from "../lib/agui";
import type { Epg, EpgProgram } from "../lib/schedule";
import { DECLINED } from "../lib/transcript";
import { CATALOG, CHANNELS, mockPlan, mockPublished, mockSchedule, summary } from "./fixtures";

/** Simulated latency; rejects with the abort reason when the run is cancelled. */
export function pause(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      window.clearTimeout(timer);
      reject(signal.reason as Error);
    });
  });
}

type Events = AsyncGenerator<AgUiEvent>;

/** What an approved CONFIRM call returns: its result, the snapshot it produces and what the agent then says. */
interface Outcome {
  readonly result: unknown;
  readonly snapshot?: AgentSnapshot;
  readonly reply: string;
}

/** A CONFIRM tool call waiting for the operator. */
interface PendingCall {
  readonly request: ApprovalRequest & { readonly tool_call_id: string };
  readonly run: (state: RunAgentInput["state"]) => Outcome;
}

type Pending = Map<string, PendingCall>;

export function mockAgent(): AgentClient {
  /** Pending approvals per conversation, like the runtime's session state. */
  const sessions = new Map<string, Pending>();
  return {
    run: (input, signal) => {
      const pending = sessions.get(input.threadId) ?? new Map<string, PendingCall>();
      sessions.set(input.threadId, pending);
      return runScript(input, pending, signal);
    },
  };
}

async function* runScript(input: RunAgentInput, pending: Pending, signal: AbortSignal): Events {
  const { threadId, runId } = input;
  yield { type: "RUN_STARTED", threadId, runId };
  const decision = input.forwardedProps.approval;
  const prompt = input.messages.at(-1)?.content ?? "";
  if (decision) {
    yield* resume(decision, input.state, pending, signal);
  } else if (/\bstop\b/i.test(prompt)) {
    yield* stopChannels(pending, signal);
  } else if (/plan|fill|schedul|publish|program/i.test(prompt)) {
    yield* planBlock(pending, signal);
  } else {
    yield* channelStatus(signal);
  }
  yield { type: "RUN_FINISHED", threadId, runId };
}

/** Answers one approval; like the runtime, then asks again for the others under the same interrupt ids. */
async function* resume(decision: ApprovalDecision, state: RunAgentInput["state"], pending: Pending, signal: AbortSignal): Events {
  const call = pending.get(decision.interrupt_id);
  if (!call) {
    yield { type: "RUN_ERROR", code: "APPROVAL_NOT_FOUND", message: `No pending approval with id ${decision.interrupt_id}.` };
    return;
  }
  pending.delete(decision.interrupt_id);
  const toolCallId = call.request.tool_call_id;
  let reply = "Understood, I left that alone. What would you like instead?";
  if (decision.approved) {
    await pause(600, signal);
    const outcome = call.run(state);
    yield { type: "TOOL_CALL_RESULT", toolCallId, content: JSON.stringify({ ok: true, ...(outcome.result as object) }) };
    if (outcome.snapshot) {
      yield { type: "STATE_SNAPSHOT", snapshot: outcome.snapshot };
    }
    reply = outcome.reply;
  } else {
    const message = `The operator declined ${call.request.tool}${decision.note ? `: ${decision.note}` : "."}`;
    const error = { code: DECLINED, message, retryable: false, hint: "Do not retry this call; ask the operator what they want instead." };
    yield { type: "TOOL_CALL_RESULT", toolCallId, content: JSON.stringify({ ok: false, error }) };
  }
  if (pending.size > 0) {
    yield* approvals(pending);
  } else {
    yield* say(reply, signal);
  }
}

async function* say(text: string, signal: AbortSignal): Events {
  const messageId = crypto.randomUUID();
  yield { type: "TEXT_MESSAGE_START", messageId, role: "assistant" };
  for (const delta of text.split(/(?<= )/)) {
    await pause(25, signal);
    yield { type: "TEXT_MESSAGE_CONTENT", messageId, delta };
  }
  yield { type: "TEXT_MESSAGE_END", messageId };
}

function* toolStart(toolCallId: string, name: string, args: object): Generator<AgUiEvent> {
  yield { type: "TOOL_CALL_START", toolCallId, toolCallName: name };
  yield { type: "TOOL_CALL_ARGS", toolCallId, delta: JSON.stringify(args) };
  yield { type: "TOOL_CALL_END", toolCallId };
}

async function* toolCall(name: string, args: object, result: unknown, signal: AbortSignal): Events {
  const toolCallId = crypto.randomUUID();
  yield* toolStart(toolCallId, name, args);
  await pause(600, signal);
  yield { type: "TOOL_CALL_RESULT", toolCallId, content: JSON.stringify(result) };
}

/** Streams a CONFIRM tool call and holds it for approval, as the runtime's ToolGuard does. */
function* confirm(pending: Pending, tool: string, args: Record<string, unknown>, reason: string, run: PendingCall["run"]): Generator<AgUiEvent> {
  const toolCallId = crypto.randomUUID();
  yield* toolStart(toolCallId, tool, args);
  const request = { interrupt_id: `v1:before_tool_call:${toolCallId}`, tool, arguments: args, reason, tool_call_id: toolCallId };
  pending.set(request.interrupt_id, { request, run });
}

function* approvals(pending: Pending): Generator<AgUiEvent> {
  for (const { request } of pending.values()) {
    yield { type: "CUSTOM", name: "approval_required", value: request };
  }
}

async function* channelStatus(signal: AbortSignal): Events {
  yield* toolCall("channels___list_channels", {}, { ok: true, channels: CHANNELS.map(summary) }, signal);
  const rows = CHANNELS.map((c) => `| ${c.name} | ${c.state.toLowerCase()} | \`${c.ladder_version}\` |`);
  const table = ["| Channel | State | Ladder |", "|---|---|---|", ...rows].join("\n");
  yield* say(`Here are your channels:\n\n${table}\n\nAsk me to plan or extend a schedule when you are ready.`, signal);
}

/** Two CONFIRM calls in one turn: two approval cards, answered one resume at a time. */
async function* stopChannels(pending: Pending, signal: AbortSignal): Events {
  yield* say("Stopping a channel takes it off air, so each stop needs your approval.", signal);
  for (const { name } of CHANNELS) {
    yield* confirm(pending, "channels___stop_channel", { channel: name }, "Stop a channel; it airs nothing until started again.", () => ({
      result: { channel: name, state: "STOPPED", alerts: [] },
      reply: `${name} is stopped.`,
    }));
  }
  yield* approvals(pending);
}

async function* planBlock(pending: Pending, signal: AbortSignal): Events {
  const channel = "fast-demo";
  const schedule = mockSchedule(channel);
  const plan = mockPlan(channel);
  const family = CATALOG.filter((item) => item.rating !== "TV-14");
  yield* say(`I'll check the published schedule of **${channel}** and fill the next block with family titles.`, signal);
  yield* toolCall("schedule___get_schedule", { channel }, { ok: true, ...schedule }, signal);
  const guide: Epg = { channel, generated_at: schedule.generated_at, ladder_version: schedule.ladder_version, programs: schedule.programs };
  yield { type: "STATE_SNAPSHOT", snapshot: { channel, plan: null, schedule: guide, schedule_scope: "channel" } };
  yield* toolCall("catalog___search_content", { ratings: ["TV-G", "TV-PG"], limit: 20 }, { ok: true, items: family }, signal);
  const picks = plan.programs.map((p) => p.asset_id);
  yield* toolCall("schedule___plan", { channel, picks, start: plan.start }, { ok: true, ...plan }, signal);
  yield { type: "STATE_SNAPSHOT", snapshot: { channel, plan, schedule: guide, schedule_scope: "channel" } };
  const breaks = plan.programs.reduce((sum, p) => sum + p.breaks.length, 0);
  yield* say(
    `The plan has **${plan.programs.length} programs** with **${breaks} ad breaks** and one warning (two minutes of filler before the block). Publishing changes what airs, so I need your approval.`,
    signal,
  );
  yield* confirm(pending, "schedule___publish", { plan_id: plan.plan_id }, "Publish a plan to the channel's live schedule; this changes what airs.", publish);
  yield* approvals(pending);
}

/** Like the agent: the published block is folded into the guide already shown, and the plan leaves the state. */
function publish(state: RunAgentInput["state"]): Outcome {
  const shown = "plan" in state ? (state as AgentSnapshot) : null;
  const plan = shown?.plan ?? mockPlan("fast-demo");
  const published = mockPublished(plan);
  const programs = foldPrograms(shown?.schedule?.programs ?? [], published.programs);
  const schedule: Epg = { channel: plan.channel, generated_at: published.generated_at, ladder_version: plan.ladder_version, programs };
  const scope = shown?.schedule ? shown.schedule_scope : null;
  return {
    result: published,
    snapshot: { channel: plan.channel, plan: null, schedule, schedule_scope: scope ?? "published" },
    reply: `Published ${published.published} programs to **${plan.channel}**; the EPG feed is updated.`,
  };
}

const foldPrograms = (guide: readonly EpgProgram[], block: readonly EpgProgram[]): EpgProgram[] => {
  const names = new Set(block.map((p) => p.name));
  return [...guide.filter((p) => !names.has(p.name)), ...block].sort((a, b) => a.start.localeCompare(b.start));
};
