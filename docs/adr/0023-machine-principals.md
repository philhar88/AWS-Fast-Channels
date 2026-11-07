# 0023 — Machine principals: the Release Controller promotes and rolls back unattended

**Status:** Accepted. Amended on 2026-11-07: the agents of the roster get machine principals of their own inside an autonomy envelope (`modules.agents`, see the amendment).

## Context

A release is promoted gradually over 30 minutes to 4 hours, and an alarm on a candidate sibling must roll it back
within seconds (P0-08: the pointer's monitor rolls back 0.4–2.8 s after the alarm; a remap reaches live sessions in
about 3 s). Waiting for a human would leave a bad release on air for as long as it takes someone to answer a page.

## Decision

- The Release Controller's roles form a machine principal (`release-controller`): it promotes and rolls back inside
  the release rules without a human approval. Every ledger record it writes names that principal
  (`release.accept`, `release.apply`, `release.point`, `release.promote`, `release.rollback`, `release.hold`,
  `release.fail`).
- Humans are notified of every promotion and rollback through the stage's alert topic; they never approve one.
- A rollback is never denied: no Budgets action or envelope rule applies to it. The ownership guard exempts only the
  controller's apply, promotion and reaper functions.
- Agents get machine principals of their own in Phase 3; until then the Release Controller is the only one.

## Consequences

- The ledger, not an approval queue, is the audit trail of releases.
- An operator who must stop a release in flight stops its execution (the pointer deployment then rolls back) or
  starts runbook R1.

## Amendment (2026-11-07): the agent roster inside an envelope

- **Principals.** Each agent of the roster (operations, programming, sales; DESIGN §19.2) that the envelope turns on is
  a machine principal: a Cognito client-credentials client whose `client_id` the Gateway's Cedar policies and the
  Runtime's authorizer see. Its ledger entries name `agent-<role>`.
- **The envelope v1** (`modules.agents.envelope`, `core.agents.envelope`) says, per agent, its level (L1 propose, L2
  act), the tools it may call unattended with their channels and per-hour caps, its actions an hour and the tools that
  always need a human. The sales agent stays L1 (Y2: commercial actions after 30 days or 20 buys propose-only).
- **Two enforcement points, both generated from it.** Cedar ENFORCE at the Gateway per tool call; and the agent's
  runner, which answers every approval-gated call from the envelope and takes its caps in one conditional DynamoDB
  transaction, never asking the LLM. A refusal is escalated to the alert topic.
- **The ledger.** Every decision and tool call is an entry of the chain `agents/<role>` with the envelope's version;
  each envelope deployed is an `envelope.apply` entry of `agents/envelope`.
- **Not yet.** The envelope is not signed (a KMS signature by the owner, DESIGN §19.3, is a follow-up), and there is
  no approval queue: what needs a human is refused and escalated.
- **Why.** The scorecard of the staffless demo (DESIGN §21.2) counts agents' actions against human touches; it needs
  agents that act, a bound on what they can do, and a record of both.
