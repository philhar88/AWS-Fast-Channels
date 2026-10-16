# 0002 — Stable L2 constructs over AWS Solutions Constructs

**Status:** Accepted

## Context
Re-use is a primary goal. AWS Solutions Constructs 2.105 offer patterns that look relevant (`aws-s3-stepfunctions`,
`aws-cloudfront-s3`, `aws-lambda-dynamodb`, `aws-eventbridge-stepfunctions`). We synthesised each against v3's needs.

## Decision
Build on stable `aws-cdk-lib` L2 constructs (CloudFront, S3, DynamoDB TableV2, Step Functions with JSONata and the
MediaConvert task, EventBridge, Cognito, AgentCore) plus thin FAST-specific constructs. Use L1 resources only where no L2
exists (MediaTailor, MediaConvert templates, log deliveries). No alpha or preview modules on the default path.

## Why not Solutions Constructs
- `S3ToStepfunctions` couples a stateful bucket and a stateless state machine in one construct (conflicts with the
  Data/Media stack split), adds a notifications custom resource, gives the rule target no DLQ or retries, and its default
  state machine fails cdk-nag SF1/SF2.
- `CloudFrontToS3` supports one origin (v3 needs S3 plus three MediaTailor origins and an ordered behaviour table) and
  uses legacy ACL-based logging.
- `LambdaToDynamoDB` does not accept `TableV2`.

## Consequences
Slightly more code in our constructs, but every property is visible and documented, and adopters can lift any single
construct without inheriting a pattern library's opinions.
