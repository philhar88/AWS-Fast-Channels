# Architecture decision records

Each record explains one decision that shaped v3: the context, what we chose, and what it costs. Evidence comes from
live experiments in an AWS account during the v3 design (October 2026). When you change one of these decisions, add a
new record that supersedes the old one rather than editing history.

| # | Decision | Status |
|---|---|---|
| [0001](0001-python-cdk-and-uv-workspace.md) | Python CDK app in a uv workspace | Accepted |
| [0002](0002-l2-constructs-over-solutions-constructs.md) | Stable L2 constructs over AWS Solutions Constructs | Accepted |
| [0003](0003-native-rendition-profiles-no-object-lambda.md) | Rendition profiles via MediaConvert AdditionalManifests, no S3 Object Lambda | Accepted |
| [0004](0004-tracing-without-the-xray-sdk.md) | Tracing without the AWS X-Ray SDK | Accepted |
| [0005](0005-preconditioned-ads.md) | Preconditioned ads (no MediaTailor transcoding) by default | Accepted |
| [0006](0006-one-cmaf-encode-per-asset.md) | One CMAF encode per asset, ladder version as the unit of compatibility | Accepted |
| [0007](0007-sigv4-channel-origin-no-cdn-shield.md) | SigV4-only Channel Assembly origin; no CDN-shielded mode in v3.0 | Accepted |
| [0008](0008-ad-break-alignment.md) | Ad breaks on segment boundaries; ESAM only for off-grid cue points | Accepted |
| [0009](0009-jwt-gateway-with-cedar-groups.md) | JWT-authenticated Gateway with Cedar policies per operator group | Accepted |
| [0010](0010-ag-ui-protocol.md) | AG-UI as the agent ↔ client protocol | Accepted |
| [0011](0011-runtime-over-harness.md) | AgentCore Runtime (Strands) over the managed Harness | Accepted |
| [0012](0012-default-model.md) | Claude Haiku 5.5 as the default model, pluggable via a model spec | Accepted |
| [0013](0013-captions-roadmap.md) | Captions are on the roadmap, not in v3.0 | Accepted |
