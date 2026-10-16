# 0011 — AgentCore Runtime (Strands) over the managed Harness

**Status:** Accepted

## Context
AgentCore Harness (GA June 2026) runs an agent loop from configuration alone. In tests a new Harness session took about
35 s to its first event, against 5–9 s for a Runtime cold session and under 0.5 s warm, and it offers no hooks for
deterministic UI state or approval interrupts.

## Decision
Use AgentCore Runtime with a Strands agent. Gateway, Policy and Memory are independent resources, so a Harness variant
can reuse them later. Agent-to-agent (A2A) integration is not in v3.0: the Gateway MCP endpoint is the integration
surface for other agents in your organisation.

## Consequences
We own a small agent application (~1,000 lines) and its tests, in exchange for latency, approvals and AG-UI state.
