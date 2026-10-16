# 0010 — AG-UI as the agent ↔ client protocol

**Status:** Accepted

## Context
The console needs streaming text, tool activity, structured state (plans, schedules) and human-approval prompts. A bespoke
event set would force every integrator to learn it.

## Decision
The runtime uses AgentCore `ProtocolType.AGUI` (verified end to end with Cognito JWT): standard AG-UI events
(`TEXT_MESSAGE_*`, `TOOL_CALL_*`, `STATE_SNAPSHOT`, `RUN_FINISHED` with interrupt outcome), plus one `CUSTOM` event,
`approval_required`. Approvals resume through `forwardedProps.approval` or the standard AG-UI resume.

## Consequences
Any AG-UI client (CopilotKit and others) can drive the agent. The console uses a small typed parser instead of
`@ag-ui/client` to avoid ~470 KB of dependencies.
