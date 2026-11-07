# The agent roster and its autonomy envelope (`modules.agents`)

The `agents` module lets agents operate the stage inside a declared envelope. The decisions are in
[ADR-0023](adr/0023-machine-principals.md). Each agent of the roster acts as its own machine principal, and the
envelope says what it may do unattended. An action outside the envelope is refused and escalated to a human.
Every action and every refusal is recorded in the ledger. The module is off by default.

| Agent | What it does | Autonomy in v1 |
|---|---|---|
| Operations | Watches alarms and runbooks, and restarts an always-on channel that stopped (runbook R12) | Acts inside the envelope (L2) |
| Programming | Keeps schedules filled: the scheduler agent's autopilot, under the envelope | Off until its pull request |
| Sales | Own-book lines and pacing | Proposes only (L1): commercial actions wait for 30 days or 20 buys |

## How it works

- **A machine principal per agent.** Each agent that is on gets a Cognito client-credentials client in the operator
  plane's user pool. Its `client_id` is the agent's principal at the Gateway and the Runtime.
- **The same agent, unattended.** A runner function holds the agent's conversation with the scheduler agent's AgentCore
  Runtime, as the autopilot does (`fast_channels.functions.agents.runner`). It uses the agent's own token, so the tools
  see the agent, not a person.
- **Two enforcement points, both generated from the envelope:**
  - **The Gateway's Cedar policies** (`infra/.../operator/cedar.py`, `agent_policies`). An agent may call READ tools and
    its grants, each only on its channels. Every other WRITE or DESTRUCTIVE tool, and every tool that needs a human,
    is forbidden.
  - **The runner.** The agent pauses before every approval-gated tool. The runner answers from the envelope
    (`core.agents.envelope.verdict`), never from the LLM, and takes the action's caps in one conditional DynamoDB
    transaction (`functions.agents.caps`). Of two concurrent restarts under a cap of one, exactly one is approved.
- **The ledger.** Every approval decision (`agent.approve`, `agent.refuse`) and every tool call (`agent.tool`, with its
  outcome) goes to the chain `agents/<role>` under the agent's principal (`agent-<role>`). Each entry carries the
  envelope's version and the trigger's correlation id. Each deploy that changes the envelope appends `envelope.apply` to
  the chain `agents/envelope`.
- **Escalation.** When anything was refused, by the runner or by Cedar, or the run failed, the runner publishes one
  message to the stage's alert topic. It also appends `agent.escalate`, with what was refused and the agent's own
  summary. The LLM never decides whether to escalate.

## The envelope v1

`modules.agents.envelope` holds the following:

- `always_on`: the channels that should always be on air (`{prefix}` is expanded).
- For each agent (`operations`, `programming`, `sales`):
  - `enabled` and `level`: `L1` proposes only, `L2` acts.
  - `grants`: each holds a tool, its `channels` (`{always_on}` stands for the always-on list) and an optional
    `per_hour` cap per channel.
  - `actions_per_hour` across all tools, and `spend_usd_per_day`. No v1 tool spends money.
  - `needs_human`: tools whose call is always refused and escalated, such as stopping, creating or deleting a channel
    (R11).

The default lets the operations agent restart each always-on channel once an hour. It lists no always-on channel, so
by default the agent restarts nothing. The shipped stage files leave `always_on` empty too. Their tests stop channels
on purpose, and the persistent stage's channels must stay stopped.

v1 is not signed. Its version is the SHA-256 of its canonical JSON. A KMS signature by the owner is a follow-up
(DESIGN §19.3).

## Turning it on

```json
"modules": {
  "ops": {"enabled": true},
  "agents": {"enabled": true, "envelope": {"always_on": ["{prefix}-demo"]}}
}
```

The module needs `ops` (the ledger and the alert topic) and the operator plane (`agent.enabled`). Every channel the
envelope names must be an IaC channel. The resources go into the Operator stack:

- each agent's client and Cedar policies;
- the runner and its dead-letter queue, with two alarms (errors, dead letters);
- the caps' table `<prefix>-agents`;
- the envelope's record function and its deploy trigger.

## Cost

About nothing while idle: an on-demand table, functions that run only when triggered, and two alarms per agent ($0.20 a
month). Each run costs a few Bedrock calls through the scheduler agent's task and its Runtime time.

## The operations agent

- **Triggers.** Two triggers start the operations agent's runner:
  - The channel census puts `Census Changed` on the stage's bus when the count of running channels drops to zero. It
    runs every 5 minutes, so it sees a stop at most one census interval later.
  - The probe canary's off-air alarm `<prefix>-probe-off-air` goes to ALARM (runbook R12's trigger). The census stops
    the canary when the last channel stops, so this alarm rarely fires for a stop.
- **Facts first.** The runner reads each always-on channel's state before any model call. When none is stopped, there
  is no conversation, and nothing is recorded but the runner's log.
- **The run.** The agent is asked to read each stopped always-on channel, start it with `channels___start_channel`, and
  read it again. The runner approves the start inside the envelope: an always-on channel, at most once an hour.
  - A second stop within the hour gets its restart refused by the cap and escalated, so a channel that keeps
    stopping comes to a human.
  - Stopping, creating or deleting a channel is never the agent's.
- **Runbook R12.** With the module, R12 knows the always-on list too, and its dry-run record says it would start the
  channel. The agent is the one that acts.
- **Auto-stop.** `channels___start_channel` keeps the deployment's auto-stop (`channels.max_runtime_hours`). An
  always-on channel the agent starts is stopped again after that many hours, and the agent restarts it at most once an
  hour. Set the auto-stop to 0 for a stage whose channels are meant to stay on.
- **Measured.** On a throwaway stage, an unplanned `StopChannel` was seen by the census and the agent 5 min 11 s later.
  The channel ran again 5 min 24 s after the stop. A second stop within the hour was refused by the cap and
  escalated, and the channel stayed stopped (`tests/e2e/test_23_agents.py`).
- **Detection limit.** The census signals only at zero running channels. On a stage where other channels still run, a
  stopped always-on channel is seen only through the probe alarm; per-channel detection is a follow-up.

## What comes next

- The programming agent: the autopilot's ticks under the envelope.
- The sales agent: hourly pacing, proposing only.
- The opt-in AWS DevOps Agent (`AWS::DevOpsAgent::AgentSpace`, read-only, advice only).

A runner can also be invoked directly with a `RunRequest` (`prompt`, `channel`, `correlation_id`, `facts`).
