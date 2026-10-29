# Architecture decision records

Each record explains one decision that shaped v3: the context, what we chose, and what it costs. Evidence comes from
live experiments in an AWS account during the v3 design (October 2026). When you change one of these decisions, add a
new record that supersedes the old one rather than editing history.

| # | Decision | Status |
|---|---|---|
| [0001](0001-python-cdk-and-uv-workspace.md) | Python CDK app in a uv workspace | Accepted, amended by 0020 |
| [0002](0002-l2-constructs-over-solutions-constructs.md) | Stable L2 constructs over AWS Solutions Constructs | Accepted, amended by 0020; L1 list amended for the modules |
| [0003](0003-native-rendition-profiles-no-object-lambda.md) | Rendition profiles are static encode manifests, no S3 Object Lambda | Accepted, amended by 0016 |
| [0004](0004-tracing-without-the-xray-sdk.md) | Tracing without the AWS X-Ray SDK | Accepted |
| [0005](0005-preconditioned-ads.md) | Preconditioned ads (no MediaTailor transcoding) by default | Accepted, amended by 0016 and 0017 |
| [0006](0006-one-cmaf-encode-per-asset.md) | One CMAF encode per asset, ladder version as the unit of compatibility | Superseded by 0016 |
| [0007](0007-sigv4-channel-origin-no-cdn-shield.md) | SigV4-only Channel Assembly origin; no CDN-shielded mode in v3.0 | Accepted |
| [0008](0008-ad-break-alignment.md) | Ad breaks on segment boundaries; frame-accurate cue points, ESAM when off the segment grid | Accepted, amended by 0016 and 0017 |
| [0009](0009-jwt-gateway-with-cedar-groups.md) | JWT-authenticated Gateway with Cedar policies per operator group | Accepted |
| [0010](0010-ag-ui-protocol.md) | AG-UI as the agent ↔ client protocol | Accepted |
| [0011](0011-runtime-over-harness.md) | AgentCore Runtime (Strands) over the managed Harness | Accepted |
| [0012](0012-default-model.md) | Claude Haiku 5.5 as the default model, pluggable via a model spec | Accepted, amended by 0029 |
| [0013](0013-captions-roadmap.md) | Captions are on the roadmap, not in v3.0 | Superseded by 0014, then 0016 |
| [0014](0014-captions-as-ladder-tracks.md) | Captions are IMSC1 tracks of the ladder: provided, embedded, then generated | Superseded by 0016 |
| [0015](0015-pluggable-captioner-elemental-inference.md) | Missing captions come from a pluggable captioner: AWS Elemental Inference smart subtitles | Superseded by 0016 |
| [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) | Plain HLS MPEG-TS, AVC + AAC muxed, Basic tier | Accepted, amended by 0017 |
| [0017](0017-ssai-only-frame-accurate-cues-with-esam.md) | SSAI only; frame-accurate ad cues with ESAM | Accepted |
| [0020](0020-demand-through-the-prebid-server-guidance.md) | Programmatic demand through the AWS Prebid Server guidance | Proposed |
| [0022](0022-delivery-states-billing-and-the-hash-chained-ledger.md) | Delivery states, counterparty billing, and the hash-chained ledger | Proposed (ledger part) |
| [0029](0029-per-task-model-registry.md) | A per-task model registry over Bedrock Converse | Accepted |
| [0031](0031-show-level-content-metadata.md) | Show-level content metadata in ad requests, looked up by the break's UPID | Proposed |
