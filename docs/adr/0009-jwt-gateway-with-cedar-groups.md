# 0009 — JWT-authenticated Gateway with Cedar policies per operator group

**Status:** Accepted

## Context
With IAM authentication between the agent runtime and the AgentCore Gateway, every Cedar decision sees the runtime's role:
policies cannot distinguish a viewer from an operator, or a person from the autopilot.

## Decision
The Gateway uses `CUSTOM_JWT` with the Cognito user pool. The runtime forwards the caller's access token to the Gateway.
Cedar policies, generated from the tool registry, permit read tools to all groups, write tools to `fast-operators` and
`fast-admins`, destructive tools to `fast-admins`, forbid the autopilot client from approval-gated tools other than
publishing schedules, and forbid destructive channel tools on IaC-owned channels. The console calls read tools on the
Gateway directly with the same token.

## Consequences
Bring your own identity provider by pointing the runtime and gateway authorizers at its discovery URL and mapping its
group claim. The policy engine runs in `ENFORCE` mode; no role in the solution can update the gateway or its policies.
