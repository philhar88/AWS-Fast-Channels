# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Report them privately through
[GitHub private vulnerability reporting](https://github.com/philhar88/AWS-Fast-Channels/security/advisories/new).
Include the affected component (construct, Lambda function, agent tool, console), the version or commit, and steps to reproduce.

You will get an acknowledgement within five working days. Fixes are released as patch versions on the
supported branches below and announced in the release notes.

If you believe you have found a vulnerability in an AWS service rather than in this project, report it to AWS
through the [AWS vulnerability reporting process](https://aws.amazon.com/security/vulnerability-reporting/).

## Supported versions

| Version | Branch | Supported |
|---|---|---|
| 3.x | `main` | ✅ |
| 2.x | `release/2.x` | ❌ (legacy, SAM) |
| 1.x | `release/1.x` | ❌ (legacy, SAM) |

## Security model in brief

This is a reference architecture you deploy into your own AWS account. Its defaults:

- All buckets are private (S3 Block Public Access, SSE-S3, TLS-only bucket policies). Viewers reach media only through
  Amazon CloudFront with origin access control; content manifests are readable only by MediaTailor.
- The Channel Assembly channel policy admits only the channel's own MediaTailor playback configuration (SigV4).
- Operators authenticate with Amazon Cognito. The agent forwards the operator's token to the AgentCore Gateway, whose
  Cedar policy engine authorises every tool call by operator group; destructive tools require human approval.
- No public Lambda function URLs; least-privilege IAM; cdk-nag (AwsSolutions, Serverless) runs on every synth.

See [docs/operations.md](docs/operations.md) and [docs/agent.md](docs/agent.md) for details and hardening guidance.
