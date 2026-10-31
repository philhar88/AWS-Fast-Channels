# 0029 — A per-task model registry over Bedrock Converse

**Status:** Accepted (amends [0012](0012-default-model.md))

## Context
[0012](0012-default-model.md) gave the scheduling agent one model spec in SSM: a model from an IAM allow-list, an
effort per mode and an output budget. Monetization adds more LLM tasks, each with its own model, data class, request
settings and cost: a rail recommender that sees viewer data, cohort rails that do not, operational digests and
proposals. Each needs its own cost and metrics, a breaker against a runaway, and a gate that keeps an untested model
out of the task.

Two routes were measured in October 2026 in us-east-1:
- **Bedrock Converse behind a per-task registry.** Every Bedrock model takes one request shape, and per-request
  fields (`outputConfig.effort`, `textFormat`, `cachePoint`, `additionalModelRequestFields`) pass through. An
  application inference profile (AIP) per task gives per-task `AWS/Bedrock` metrics (`ModelId` is the profile's id)
  and a cost allocation tag, with no gateway in the path.
- **The AgentCore Gateway inference route.** It serves InvokeModel and the Messages API, not Converse. Rules and
  fallback work through the API, but CloudFormation does not have them (`GatewayRule` has no `MatchModels`,
  `StaticRoute.Model` or `Fallback`). Under 8 minutes of sustained load its token limits let through about four
  times their rate. The Messages dialect answers HTTP 200 with an internal error body for unknown ids and for AIP
  ARNs, and the route adds about 80–90 ms per call.

A replay of the rail recommender (300 synthetic viewers, 206 calls per arm) gave these results:
- Claude Haiku 5.5 at its default (adaptive) thinking and `maxTokens` 4,000 returned empty output with
  `stopReason=max_tokens` on 29% of rails. With thinking disabled, or with a larger budget, every call returned a
  valid rail.
- Changing the output schema or the thinking setting between calls rewrote the prompt cache. A per-call
  `content_id` enum tripled the cost per rail.

Building the registry (M13-01 to M13-03) measured two more facts:
- A role needs the AIP, plus the foundation model through it (`bedrock:InferenceProfileArn`), to call Converse
  through the AIP. No statement on the system profile is needed. A role granted one AIP is denied any other AIP
  and the system profile itself, and prompt caching works through an AIP.
- AWS Budgets refuses an action on a daily budget ("AWS Budgets Actions don't support daily granularity budget for
  now").

## Decision
- **One registry entry per task.** Each task has the SSM parameter `/<prefix>/models/tasks/<task>`, rendered by the
  `Models` stack from `modules.models.tasks` in `cdk.json`. The stack is deployed before the Operator stack. Every
  reader validates the entry: `model`, `profile_arn`, `route`, `effort`, `thinking`, `max_tokens`, `data_class`,
  `daily_cap_usd`, `eval` (`contracts/model-registry.schema.json`). The scheduling agent's task is
  `scheduler.plan`, and Claude Haiku 5.5 stays its model.
- **Route `bedrock` (Converse) is the only route built.** The AgentCore Gateway inference route is documented as an
  opt-in alternative and is never the default.
- **Models are fixed at deployment.** An entry may name only a model in `agent.allowed_models`.
  - Each task gets its own AIP `<prefix>-<task>`, copied from the system inference profile of its model and tagged
    `fast:task=<task>`.
  - The task's role may invoke only that profile, and the foundation model only through it.
  - Swapping a task's model is a deployment (a new profile), never an SSM edit. With the registry on, the
    scheduler's ModelSpec parameter is gone. An SSM edit of the entry tunes effort, thinking, `max_tokens` and the
    cap. When the entry cannot be used, the entry as deployed applies.
- **Viewer data stays in its geography.** A task with `data_class: viewer` must use a geographic profile (`us.`,
  `eu.`, ...), never `global.`.
- **Thinking and the output schema never change per call.** `thinking` is a field of the task: `adaptive` omits it,
  `disabled` turns it off. The recommender's default is Claude Haiku 5.5 with thinking on (`adaptive`) and
  `max_tokens` 8,000.
- **Caps are circuit breakers.** They are set at about ten times expected use, and the owner treats cost as no
  issue.
  - A deterministic token counter per task and UTC day (`<prefix>-models`) admits a run and each model call only
    while the day's spend at list prices is under `daily_cap_usd`. Past the cap the run ends with
    `MODEL_CAP_REACHED`, and the alert topic is told once a day. An unreachable counter lets calls through.
  - An optional AWS Budget per task role (`budgets_action`) denies the role's invocations of its profile. The
    budget is monthly, at 31 days of the cap, because Budgets actions accept no daily budget.
- **Eval gate.** `make eval TASK=<task> MODEL=<id>` runs the task's conformance suite with the task's settings and
  writes `evals/records/<task>/<key>.json` (`contracts/eval-record.schema.json`), which is committed.
  - With the registry on, synth fails when a task's `eval` names no passing record for its model family and
    settings (effort, thinking, `max_tokens`) with the checks its task requires. Any routing of the tested model
    qualifies.
  - The recommender's record must hold `schema_valid`, `faithfulness` (the deterministic check that tile reasons
    cite only input facts), `policy_violations` and `cost_per_rail`. A judge sample runs before any default flip,
    and a swap on a viewer-data task needs human approval.

## Consequences
- Each task's tokens, latency, throttles and cost show up per task in `AWS/Bedrock`. With the `fast:task` cost
  allocation tag activated in the payer account, they also show up in Cost Explorer and AWS Budgets.
- With `modules.models` off nothing changes: the agent keeps the v3.0.0 model spec.
- Switching a task to another allowed model takes a new eval record, a `cdk.json` edit and a deploy. Another
  Bedrock model family also needs the allow-list. Whether it supports structured output is unknown until its eval
  proves it.
- The counter adds one conditional DynamoDB write per run and per model call, a few milliseconds each.
- Strands keys some defaults on the model id string (prompt caching, tool-result status, context window), so with
  an AIP the agent sets them from the underlying model.
- Candidate AWS defects found on the gateway route, and the undocumented daily-budget limit of Budgets actions, go
  to the owner's AWS letters, not into code. The gateway defects are: Messages answering 200 with an error body;
  rate-limit model keys resolved after the rule pin; token limits letting too much through; an undocumented 10×
  output burndown for Haiku and Sonnet 5.5.
