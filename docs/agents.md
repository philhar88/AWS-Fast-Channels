# The agent roster and its autonomy envelope (`modules.agents`)

The `agents` module lets agents operate the stage inside a declared envelope. The decisions are in
[ADR-0023](adr/0023-machine-principals.md). Each agent of the roster acts as its own machine principal, and the
envelope says what it may do unattended. An action outside the envelope is refused and escalated to a human.
Every action and every refusal is recorded in the ledger. The module is off by default.

| Agent | What it does | Autonomy in v1 |
|---|---|---|
| Operations | Watches alarms and runbooks, and restarts an always-on channel that stopped (runbook R12) | Acts inside the envelope (L2) |
| Programming | Keeps programmed channels' schedules filled at least 2 hours ahead (runbook R6): the scheduler agent's autopilot, under the envelope | Acts inside the envelope (L2); nearer edits are a human's |
| Sales | Watches the own book's pacing and proposes changes to its lines (with `modules.pds`) | Proposes only (L1): commercial actions wait for 30 days or 20 buys |

The opt-in [AWS DevOps Agent](#the-devops-agent) investigates and advises beside them; it never acts.

## How it works

- **A machine principal per agent.** Each agent that is on gets a Cognito client-credentials client in the operator
  plane's user pool. Its `client_id` is the agent's principal at the Gateway and the Runtime.
- **The same agent, unattended.** A runner function holds the agent's conversation with the scheduler agent's AgentCore
  Runtime, as the autopilot does (`fast_channels.functions.agents.runner`). It uses the agent's own token, so the tools
  see the agent, not a person.
- **Facts first.** A trigger becomes a conversation only when the runner, before any model call, reads that there is
  something to do: a stopped always-on channel, a programmed channel whose schedule runs out within 6 hours, an
  off-pace line.
- **Three enforcement points, all generated from the envelope:**
  - **The Gateway's Cedar policies** (`infra/.../operator/cedar.py`, `agent_policies`). An agent may call READ tools and
    its grants, each only on its channels. Every other WRITE or DESTRUCTIVE tool, and every tool that needs a human,
    is forbidden. At L1 every approval-gated tool is forbidden. Cedar also filters `tools/list` by caller, so an agent
    never sees a tool outside its grants.
  - **The Gateway's request interceptor** (`fast_channels.functions.agents.interceptor`). The Gateway passes no caller
    identity to its Lambda targets, but its REQUEST interceptor gets each request with the validated token. For every
    non-READ `tools/call` an agent's token makes, it applies the envelope (`functions.agents.enforce`):
    - the verdict (`core.agents.envelope.verdict`);
    - the publish lead, read from the stored plan;
    - the caps, which a call inside the envelope takes in one conditional DynamoDB transaction
      (`functions.agents.caps`). Of two concurrent restarts under a cap of one, exactly one reaches the tool.

    A call outside the envelope never reaches the tool: the Gateway answers with the tools' error envelope, code
    `ENVELOPE`. So the limits hold however the agent's token reaches the Gateway, with or without the runner.
    The interceptor runs before Cedar: on a throwaway stage the sales agent's token calling
    `channels___start_channel` straight to the Gateway was refused there (`propose_only`), recorded and escalated.
    Operators' and the autopilot's calls, READ tools and other MCP methods pass untouched, with no AWS call.
  - **The runner.** The agent pauses before every approval-gated tool. The runner answers from the same verdict, with
    a read of the caps, never from the LLM. The usual refusal is therefore recorded before the call is made.
- **The ledger.** Everything goes to the chain `agents/<role>` under the agent's principal (`agent-<role>`):
  - every approval decision of the runner (`agent.approve`, `agent.refuse`);
  - every tool call (`agent.tool`, with its outcome);
  - every non-READ call the interceptor let through or refused (`agent.allow`, `agent.refuse`, with
    `source: gateway`).

  Each entry carries the envelope's version and the trigger's correlation id. Each deploy that changes the envelope
  appends `envelope.apply` to the chain `agents/envelope`.
- **Escalation.** The runner publishes one message to the stage's alert topic and appends `agent.escalate` in four
  cases: anything was refused (by the runner or by Cedar), the run failed, the facts still show the problem after the
  run (a programmed channel still short of runway), or a propose-only agent proposed something.
  The message carries what was refused or proposed, and the agent's own summary. A call the interceptor refused, the
  interceptor escalates itself. The LLM never decides whether to escalate.

## The envelope v1

`modules.agents.envelope` holds the following:

- `always_on`: the channels that should always be on air (`{prefix}` is expanded).
- `programmed`: the channels the programming agent keeps scheduled. A programmed channel has no `autopilot_cron`, so
  each channel has one writer.
- For each agent (`operations`, `programming`, `sales`):
  - `enabled` and `level`: `L1` proposes only, `L2` acts.
  - `grants`: each holds a tool, its `channels` and two optional fields:
    - `channels`: `{always_on}` and `{programmed}` stand for the lists above.
    - `per_hour`: a cap per channel (or per tool, for a tool that names no channel).
    - `lead_minutes`: how far ahead a published plan must start.
  - `actions_per_hour`: approval-gated actions across all tools.
  - `spend_usd_per_day`. No v1 tool spends money.
  - `needs_human`: tools whose call is always refused and escalated, such as stopping, creating or deleting a channel
    (R11).

The defaults:

| Agent | Default |
|---|---|
| Operations | Restarts each always-on channel once an hour |
| Programming | Off. Plans and publishes on its programmed channels, at most 6 publishes a channel an hour, each plan starting at least 120 minutes ahead. Removing programs and editing a brief need a human. |
| Sales | Off. Records at most 12 proposals an hour. |

The default envelope lists no always-on or programmed channel, so the agents act on nothing until a stage names one. The
shipped stage files, apart from the roster's own e2e stage, leave both lists empty. Their tests stop channels and
publish schedules on purpose, and the persistent stage's channels must stay stopped.

v1 is not signed. Its version is the SHA-256 of its canonical JSON. A KMS signature by the owner is a follow-up
(DESIGN §19.3).

## Turning it on

```json
"modules": {
  "ops": {"enabled": true},
  "agents": {
    "enabled": true,
    "envelope": {
      "always_on": ["{prefix}-demo"],
      "programmed": ["{prefix}-demo"],
      "agents": {"programming": {"enabled": true}}
    }
  }
}
```

The module needs `ops` (the ledger and the alert topic) and the operator plane (`agent.enabled`). The sales agent also
needs `modules.pds`. Every channel the envelope names must be an IaC channel. The resources go into the Operator stack:

- each agent's client and Cedar policies;
- each agent's runner and its dead-letter queue, with two alarms (errors, dead letters);
- the Gateway's request interceptor;
- the caps' table `<prefix>-agents`;
- the envelope's record function and its deploy trigger;
- with the sales agent, the Gateway target `sales`.

## Cost

About nothing while idle: an on-demand table, functions that run only when triggered, and two alarms per agent ($0.20 a
month). The interceptor runs once per Gateway request, a few milliseconds when the caller is not an agent. Each agent
run costs a few Bedrock calls through the scheduler agent's task, and its Runtime time.

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
  escalated, and the channel stayed stopped. A start with the agent's token straight to the Gateway, with no runner,
  was refused there by the same cap (`tests/e2e/test_23_agents.py`).
- **Detection limit.** The census signals only at zero running channels. On a stage where other channels still run, a
  stopped always-on channel is seen only through the probe alarm; per-channel detection is a follow-up.

## The programming agent

- **Trigger.** An hourly rule (minute 0) starts the programming agent's runner.
- **Facts first.** The runner reads where each programmed channel's schedule ends (Channel Assembly's
  `GetChannelSchedule`, programs only). When every one reaches 6 hours ahead, there is no conversation.
- **The run.** For each short channel the agent is asked to read the brief and the schedule, find assets, and extend
  the schedule to 8 hours ahead with `schedule___plan` and `schedule___publish`.
  - The first new block starts where the schedule ends, or 2 hours and 10 minutes from now if that is later.
  - The runner approves each publish whose stored plan starts at least 120 minutes ahead, at most 6 a channel an hour.
  - A gap nearer than 2 hours stays a human's: its publish is refused by the runner, and by the interceptor if the
    agent's token publishes it directly.
- **Facts after.** After the run the runner reads the runway again. A channel still short is escalated to a human,
  though nothing was refused: for example when the agent found no asset it would air. On a throwaway stage it
  declined to put the e2e test pattern on air until the channel's brief said, in its `notes`, that the test pattern is
  the channel's programming.
- **One writer.** A programmed channel has no autopilot schedule (configuration check); the autopilot stays the v3
  feature for the other channels.
- **Measured.** On a throwaway stage with an empty schedule, the hourly rule woke the agent, which published three
  blocks covering 2 to 8 hours ahead. The runner approved each publish, and the interceptor let each through. The
  next run found 7 h 48 min of runway and did nothing.
  - **The fault.** An operator then removed the upcoming programs. The agent published three blocks again, the first
    starting 2 h 10 min after the trigger. Its fourth publish in that UTC hour was refused by the cap of 6 and
    escalated.
  - **A block 30 minutes ahead.** It was refused at the approval and escalated. Publishing it with the agent's token
    straight to the Gateway was refused there (`tests/e2e/test_23_agents.py`).

## The sales agent

- **Tools.** With `modules.pds`, the Gateway gets a `sales` target with two tools (`fast_channels.functions.tools.sales`).
  Operators may call them too.
  - `sales___line_pacing` (READ): every own-book line of the published PDS snapshot, the demo lines and the buys
    booked over AdCP. For each it gives the budget, the spend from the line's budget shards (reserved and delivered),
    the flight, and the pace: the share of the budget spent over the share of the flight gone (`core.pds.pacing`).
    Above 1.2 a line is overpacing, below 0.8 underpacing, and in the first 5% of its flight its pace is not judged.
    A demo line gets a flight with `starts` and `ends` (`modules.pds.demo_lines`).
  - `sales___propose` (WRITE): records a proposal about one line in the ledger chain `sales/proposals` (kind
    `proposal`), with the line's pacing at the time: pause it, end or extend its flight, change its budget, or cap it
    per session. It changes nothing. The Gateway does not say who called a tool, so the proposal's entry names the
    tool. The interceptor's `agent.allow` for the same call names the agent.
- **Trigger.** An hourly rule (minute 30). The runner reads every line's pacing first, and only an off-pace line
  starts a conversation.
- **Propose only.** The agent is asked to record one proposal per off-pace line. It has no tool that commits a change:
  Cedar forbids it every approval-gated tool and hides them from its `tools/list`. Every proposal is escalated to a
  human, who changes the line's configuration or its AdCP buy.
- **Measured.** On a throwaway stage with the PDS, a demo line's budget shards were spent to 34% with 17% of its
  flight gone, a pace of 2.0.
  - The agent read the pacing and recorded a `pause` proposal for the line, and the runner escalated it.
  - The sales agent's token calling `channels___start_channel` straight to the Gateway was refused there
    (`propose_only`) and escalated (`tests/e2e/test_23_agents.py`, `STAGE=fastcipace`).

## The DevOps Agent

`modules.agents.devops_agent: true` adds AWS DevOps Agent beside the roster
(`infra/.../operator/devops_agent.py`):

- **The Agent Space.** It is named `<prefix>-devops`. Elevated actions are off, and there is no operator app: a human
  turns on the DevOps Agent web app with their own identity provider.
- **Its role.** The role is trusted by `aidevops.amazonaws.com` for this account's agent spaces only. It has the AWS
  managed `AIDevOpsAgentAccessPolicy`, the reads an investigation makes, plus the Resource Explorer service-linked
  role it creates. An explicit deny of `support:CreateCase`, that policy's one write, keeps it to advice.
- **The association.** The account is associated as the `monitor` account, with that role and no elevated role.

The agent investigates and recommends; it has no role to act with. In v1 nothing feeds its findings to the roster
automatically.

The first Agent Space of an account creates the service-linked role `AWSServiceRoleForAIDevOps`, which stays when
the stack is destroyed. Once no agent space is left in the account, delete it with
`aws iam delete-service-linked-role --role-name AWSServiceRoleForAIDevOps`. On a throwaway stage the space was
created and deleted cleanly with the Operator stack.

AWS DevOps Agent runs in us-east-1, us-west-2, ap-southeast-2, ap-northeast-1, eu-central-1 and eu-west-1 (a
configuration check). Idle, it costs nothing. Its investigations, evaluations and on-demand tasks are billed per
agent-second, 0.0083 US dollars, about 30 US dollars an agent-hour.

## What comes next

- Per-channel stop detection: the census bounds MTTD at its 5-minute interval and signals only at zero.
- A KMS-signed envelope (DESIGN §19.3).
- The other scorecard faults: `/policy` 5xx (R3), PBS 5xx, a `NEW_CREATIVE` spike (R4), consent lag (R8), a
  throttled redeem; and R6's under-30-minutes evergreen block.
- Commercial L2 after 30 days or 20 buys, an approval queue in the console, and the scorecard dashboard.
- Eval suites for the agents' runs beyond conformance.

A runner can also be invoked directly with a `RunRequest` (`prompt`, `channel`, `correlation_id`, `facts`).
