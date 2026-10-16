/**
 * A scripted agent speaking the same AG-UI events as the runtime: tool activity, a STATE_SNAPSHOT plan, an
 * `approval_required` interrupt before publishing, and the resumed run after the operator decides.
 */
import type { AgentClient, AgentSnapshot, AgUiEvent, RunAgentInput } from "../lib/agui";
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

export function mockAgent(): AgentClient {
  return { run: (input, signal) => runScript(input, signal) };
}

async function* runScript(input: RunAgentInput, signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  const { threadId, runId } = input;
  yield { type: "RUN_STARTED", threadId, runId };
  const approval = input.forwardedProps.approval;
  const prompt = input.messages.at(-1)?.content ?? "";
  if (approval) {
    yield* approval.approved ? publish(input.state, signal) : say("Understood, nothing was published. The plan stays available for an hour if you change your mind.", signal);
  } else if (/plan|fill|schedul|publish|program/i.test(prompt)) {
    yield* planBlock(signal);
  } else {
    yield* channelStatus(signal);
  }
  yield { type: "RUN_FINISHED", threadId, runId };
}

async function* say(text: string, signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  const messageId = crypto.randomUUID();
  yield { type: "TEXT_MESSAGE_START", messageId, role: "assistant" };
  for (const delta of text.split(/(?<= )/)) {
    await pause(25, signal);
    yield { type: "TEXT_MESSAGE_CONTENT", messageId, delta };
  }
  yield { type: "TEXT_MESSAGE_END", messageId };
}

async function* toolCall(name: string, args: object, result: unknown, signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  const toolCallId = crypto.randomUUID();
  yield { type: "TOOL_CALL_START", toolCallId, toolCallName: name };
  yield { type: "TOOL_CALL_ARGS", toolCallId, delta: JSON.stringify(args) };
  yield { type: "TOOL_CALL_END", toolCallId };
  await pause(600, signal);
  yield { type: "TOOL_CALL_RESULT", toolCallId, content: JSON.stringify(result) };
}

async function* channelStatus(signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  yield* toolCall("channels___list_channels", {}, { ok: true, channels: CHANNELS.map(summary) }, signal);
  const lines = CHANNELS.map((c) => `- **${c.name}** is ${c.state.toLowerCase()} on ladder \`${c.ladder_version}\``);
  yield* say(`Here are your channels:\n\n${lines.join("\n")}\n\nAsk me to plan or extend a schedule when you are ready.`, signal);
}

async function* planBlock(signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  const channel = "fast-demo";
  const schedule = mockSchedule(channel);
  const plan = mockPlan(channel);
  const family = CATALOG.filter((item) => item.rating !== "TV-14");
  yield* say(`I'll check the published schedule of **${channel}** and fill the next block with family titles.`, signal);
  yield* toolCall("schedule___get_schedule", { channel }, { ok: true, ...schedule }, signal);
  yield { type: "STATE_SNAPSHOT", snapshot: { channel, plan: null, schedule } };
  yield* toolCall("catalog___search_content", { ratings: ["TV-G", "TV-PG"], limit: 20 }, { ok: true, items: family }, signal);
  const picks = plan.programs.map((p) => p.asset_id);
  yield* toolCall("schedule___plan", { channel, picks, start: plan.start }, { ok: true, ...plan }, signal);
  yield { type: "STATE_SNAPSHOT", snapshot: { channel, plan, schedule } };
  const breaks = plan.programs.reduce((sum, p) => sum + p.breaks.length, 0);
  yield* say(
    `The plan has **${plan.programs.length} programs** with **${breaks} ad breaks** and one warning (two minutes of filler before the block). Publishing changes what airs, so I need your approval.`,
    signal,
  );
  yield {
    type: "CUSTOM",
    name: "approval_required",
    value: {
      interrupt_id: crypto.randomUUID(),
      tool: "schedule___publish",
      arguments: { plan_id: plan.plan_id },
      reason: "Publish a plan to the channel's live schedule; this changes what airs.",
    },
  };
}

/** Like the agent: the published block replaces the plan in the shared state (re-read the EPG for the guide). */
async function* publish(state: RunAgentInput["state"], signal: AbortSignal): AsyncGenerator<AgUiEvent> {
  const snapshot = "plan" in state ? (state as AgentSnapshot) : null;
  const plan = snapshot?.plan ?? mockPlan("fast-demo");
  const published = mockPublished(plan);
  yield* toolCall("schedule___publish", { plan_id: plan.plan_id }, { ok: true, ...published }, signal);
  yield { type: "STATE_SNAPSHOT", snapshot: { channel: plan.channel, plan: null, schedule: published } };
  yield* say(`Published ${published.published} programs to **${plan.channel}**; the EPG feed is updated.`, signal);
}
