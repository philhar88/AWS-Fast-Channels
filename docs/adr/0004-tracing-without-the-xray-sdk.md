# 0004 — Tracing without the AWS X-Ray SDK

**Status:** Accepted

## Context
Every service must log to CloudWatch and trace where possible. The AWS X-Ray SDKs and daemon entered maintenance mode on
25 February 2026 (end of support 25 February 2027); Powertools for AWS Lambda's Tracer still depends on the X-Ray SDK.
The OpenTelemetry Lambda layer works but added 0.6–0.8 s to every cold start in our measurements.

## Decision
- Lambda functions and state machines use **active tracing** (service-generated segments) and Powertools Logger with a
  correlation id on every line; no new code depends on the X-Ray SDK.
- The AgentCore Runtime uses the AWS Distro for OpenTelemetry (`opentelemetry-instrument`), which is the AgentCore
  observability path; spans need CloudWatch Transaction Search, an account-level opt-in (`AccountBaseline` stack).
- Joins across services that do not trace (MediaTailor, Channel Assembly, CloudFront) use ids: MediaTailor session id,
  SCTE-35 event id and UPID, CloudFront request id, Step Functions execution name. Saved Logs Insights queries do the joins.

## Consequences
Downstream SDK calls inside a Lambda are not individual subsegments. When Powertools ships an OpenTelemetry tracer, adopt
it behind the same correlation contract.
