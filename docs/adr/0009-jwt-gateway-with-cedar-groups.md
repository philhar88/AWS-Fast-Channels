# 0009 — JWT-authenticated Gateway with Cedar policies per operator group

**Status:** Accepted

## Context
With IAM authentication between the agent runtime and the AgentCore Gateway, every Cedar decision sees the runtime's role:
policies cannot distinguish a viewer from an operator, or a person from the autopilot.

## Decision
The Gateway uses `CUSTOM_JWT` with the Cognito user pool. The runtime forwards the caller's access token to the Gateway.
Cedar policies, generated from the tool registry, permit read tools to all groups, write tools to `fast-operators` and
`fast-admins`, destructive tools to `fast-admins`, and forbid destructive channel tools on IaC-owned channels. When a
channel has an autopilot schedule, an autopilot machine client exists too: it may call read tools and plan and publish
on its own channels only, and is forbidden every other approval-gated tool. The console calls read tools, and its
Start and Stop buttons, on the Gateway directly with the same token.

## Consequences
Bring your own identity provider by pointing the runtime and gateway authorizers at its discovery URL and mapping its
group claim. The policy engine runs in `ENFORCE` mode; no role in the solution can update the gateway or its policies.
Policy names are unique per account, so each is deployed as `<engine>_<policy>`. The Gateway passes no caller
identity to the tool functions, so the record of who changed what is the Gateway's application logs, the tools'
`tool audit` records and, for agent runs, the runtime's `run started` record; console calls straight to the Gateway
have no recorded actor.
