# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [3.0.0] - 2026-10-16

A rewrite of FAST Channels on AWS on the AWS CDK (Python), with agentic scheduling on Amazon Bedrock AgentCore, one
plain HLS encode per asset (MPEG-TS, AVC with AAC muxed, MediaConvert Basic tier), frame-accurate ad cues, SSAI, and
end-to-end traceability. v2 deployments are not upgraded in place; see
[docs/migration-from-v2.md](docs/migration-from-v2.md).

### BREAKING

- The AWS SAM templates (`deployment/`), the SAM Lambda functions and layers (`source/`) and the CloudFormation
  parameters are gone. Deploy with `make deploy`; configure in the `fast-channels` block of `cdk.json`.
- AWS Elemental MediaPackage VOD is no longer used. Channel Assembly reads the encode's HLS manifests from S3 with
  SigV4. Content encoded or packaged by v2 cannot be scheduled on v3 channels and must be re-encoded.
- Stack, bucket and resource names, playback URLs and outputs are new.
- The default ladder (`standard`) is H.264 with AAC muxed, up to 1080p. v3 has no HEVC, 4K or DASH output.
- Ads are preconditioned by default (MediaTailor `AdConditioning` `NONE`).
- The per-asset notification email with playback URLs is replaced by EventBridge events and failure alerts.
- Python 3.14 on arm64 and Node.js 24 are required.

### Added

- **Operator plane on Amazon Bedrock AgentCore**: a Strands scheduling agent on AgentCore Runtime (AG-UI protocol,
  Cognito JWT authorizer), an AgentCore Gateway (MCP, CUSTOM_JWT) with 27 tools in six Lambda targets (`catalog`,
  `library`, `origins`, `schedule`, `channels`, `encoding`), a Cedar policy engine in `ENFORCE` mode generated from
  the tool registry, human approval for the 11 tools that change what airs or cost money, AgentCore Memory (sessions
  and operator preferences, tool traffic excluded from extraction), and agent telemetry.
- **Operator console** (React, Cloudscape, hls.js): chat with tool activity and approval cards; an SSAI player with
  ad events; EPG timeline; channel start and stop; library and ladders.
- **Autopilot**: per-channel EventBridge Scheduler schedules that keep a channel's schedule 24 hours ahead with a
  machine token limited by Cedar to planning and publishing on its own channels. The machine client, its permits and
  the autopilot's error and dead-letter alarms exist only when a channel has an `autopilot_cron`.
- **Model choice**: Claude Haiku 5.5 by default, as the deployment region's inference profile (`{geo}` resolves to
  `us.`, `eu.`, `au.` or `global.`); a validated ModelSpec in SSM switches among allow-listed models without a
  redeploy; `make model-access` checks Bedrock access; `make eval` runs the scheduling conformance suite.
- **Ladders as code**: validated, fingerprinted TOML ladders (`standard`) compiled to MediaConvert job templates;
  agent-driven ladder proposals (cap or floor the height, shift quality, drop rungs) and provisioning; encode cost
  estimates.
- **One plain HLS encode** per asset: a MediaConvert Apple HLS output group of MPEG-TS segments, each variant H.264
  High with AAC-LC 128 kbit/s stereo muxed in (`avc-720p` first, `avc-1080p`, `avc-360p`), 29.97 fps, 2 s closed GOPs,
  6.006 s segments and `CLOSED-CAPTIONS=NONE`, on the Basic tier at about $0.0375 per content minute. The encode's own
  `main.m3u8` is the one rendition profile, `hls`.
- **Frame-accurate ad cues**: cue points snap to the nearest frame; a point between segment boundaries adds ESAM to
  that title's job, which splits every variant at that frame and bills the job on the Professional tier (about $0.06
  per content minute).
- **Ingest pipeline** (Step Functions, JSONata): uploads with JSON sidecars, `Ingest Requested` events or tool calls;
  probe-based refusals (HDR, 50/60 Hz conversions); cue points snapped to the nearest frame; output verification
  against the ladder signature, nominal `BANDWIDTH`, break-offset discovery; VOD source registration. An encode that
  is `READY` is never overwritten in place, a reused correlation id is reported (`DuplicateCorrelationId`), and every
  refusal or failure ends in an `Asset Failed` event.
- **Ladder kits**: filler slate, four break slates, two house ads and a VAST 4.2 pod per ladder version, encoded at
  deploy time or by the agent.
- **Channels**: IaC LINEAR channels with per-channel `STITCHED_ONLY` playback configurations, SigV4-only channel
  policies, update guards and lifecycle handling; agent-created channels from the same specifications; auto-stop after
  a maximum runtime.
- **Schedules**: deterministic planning with programming briefs (ratings, genres, dayparts and a break policy whose
  minimum gap defaults to half the cadence, with start and end margins and a minimum program length that editorial
  cue points are exempt from), SCTE-35 `TIME_SIGNAL` breaks with deterministic ids, idempotent and resumable
  publishing with leases and stale-plan detection, every program `ABSOLUTE` at its planned start, the schedule of
  record in DynamoDB (kept 30 days after each program ends), EPG feeds in JSON and XMLTV, as-run reconciliation.
  Starting a stopped channel late removes the programs whose start has passed (reported as `skipped`, `SKIPPED` in
  the guide) and airs the rest at their planned times.
- **Bring-your-own content**: agent tools to create SigV4 source locations for your buckets, register pre-encoded
  content after a ladder-signature check, and update package configurations.
- **Delivery**: one CloudFront distribution (HTTP/2 and HTTP/3, IPv6, OAC) whose behaviours come from one routing
  table, published as a CDN-neutral path contract; optional CDN-neutral content domain; standard logs v2 in Parquet
  with CMCD fields.
- **Observability**: logs with retention for every service, one correlation id from upload to ad request, saved
  Logs Insights queries, `scripts/trace.py`, a dashboard, alarms (including Channel Assembly quota usage, running
  channel-hours and dead-letter queues) and SNS alerts; a `tool audit` record for every change made through a tool;
  optional CloudWatch Transaction Search for agent traces and online evaluations. Prompts and model replies stay out
  of the agent's logs and spans unless `observability.capture_prompts` is on.
- **Contracts**: catalog OpenAPI for bring-your-own CMS (also as `catalog.gateway.openapi.json`, ready for an
  AgentCore Gateway OpenAPI target), `IngestRequest` and event JSON schemas, generated from the domain models.
- **Teardown**: a runtime-resource reaper that deletes runtime-created resources before their containers, draining
  channels within each invocation's time budget so an interrupted teardown resumes; `scripts/cleanup.py` for
  teardowns that cannot finish.
- **Engineering**: uv workspace (`core`, `functions`, `agent`, `infra`), ruff, strict mypy (also over `scripts/` and
  `tests/e2e`), unit and CDK assertion tests, cdk-nag (AWS Solutions and Serverless) on every synth, a manual
  end-to-end release workflow, architecture decision records, Dependabot.

### Changed

- Encoding moved from a CSV-generated preset list to a validated ladder with 2 s GOPs, 6 s segments, QVBR and
  frame-rate normalisation.
- Ad-break placement keeps the v2 `AdOffsets` S3 object tag and adds sidecar and catalog cue points.
- Tracing uses Lambda and Step Functions active tracing and AWS Distro for OpenTelemetry instead of the X-Ray SDK.
- Documentation rewritten: README and `docs/` (architecture, encoding, Channel Assembly, ad insertion, delivery,
  agent, CMS integration, observability, operations, migration).

### Security

- The media bucket admits Channel Assembly only as the deployment's own source location, `<prefix>-media`, and the
  reaper's IAM reaches only `<prefix>-*` names, so one deployment's grants never match another's names (`fast` and
  `fastci`, for example).
- The autopilot's Cognito client, whose secret `cognito-idp:DescribeUserPoolClient` returns, exists only with an
  autopilot schedule, and its Cedar permits name the channels it schedules.
- The console accepts a local dev server as a sign-in redirect only with `agent.console_dev_url`.
- The Gateway's service role trusts only this deployment's Gateway; the alert topic admits no service principal
  (EventBridge publishes through a rule-scoped role); the agent runtime cannot read the CDK assets bucket beyond its
  own code.
- The end-to-end workflow validates its `prefix` input and passes it to commands only through the environment.

### Removed

- AWS SAM templates and the nested MediaConvert application.
- MediaPackage VOD packaging group and configurations, the Secrets Manager access token and the MediaTailor KMS key.
- The boto3, crhelper and xmltodict Lambda layers, and the SNS email sender function.
- `assets/presets.csv` and its preset generator scripts.

### Changed since the first v3.0.0 tag

- **Plain HLS MPEG-TS, AVC + AAC muxed, Basic tier**, for cost and stability
  ([ADR-0016](docs/adr/0016-plain-hls-ts-avc-aac-basic-tier.md)). The first tag encoded one CMAF group of AV1, HEVC
  and AVC with HLS and DASH manifests per rendition profile and IMSC1 captions, on the Professional tier at about
  $0.56 per content minute; every feature brought MediaConvert, MediaTailor or hls.js behaviour to work around. The
  encode is now three AVC variants with AAC muxed in, about $0.0375 per content minute. Gone: the `uhd` ladder,
  rendition profiles other than `hls`, DASH outputs and playback URLs, HEVC, AV1, audio rendition groups, I-frame
  playlists, accelerated transcoding, loudness normalisation, and captions (the `captions.*` settings, the Elemental
  Inference captioner, caption fields in requests, events and tools, and the console's CC toggle). Every ladder
  version changes: kits re-provision and assets re-encode.
- **SSAI only** ([ADR-0017](docs/adr/0017-ssai-only-frame-accurate-cues-with-esam.md)). The first tag served SSAI and
  SGAI (HLS Interstitials) from `PLAYER_SELECT` playback configurations; they are now `STITCHED_ONLY`. Gone:
  `?aws.insertionMode=GUIDED` URLs, asset lists, client-side beacons, the `/preconditioned-ads/*` and `/v1/i-media/*`
  CloudFront routes, and the console's SGAI player. Playback URLs (the `Channels` output, `channels___get_channel`,
  `core.specs.playback_urls`) are one SSAI URL per output instead of `{ssai, sgai}`. Preconditioned house ads stay:
  their manifest copies under `ads/` are the VAST `MediaFile`s, and MediaTailor stitches their segments through
  `/v1/segment` redirects. CloudFront follows MediaTailor's production configuration: one uncached `/v1/*` route
  (it replaces `/v1/manifest/*` and `/v1/*`) forwards every viewer header, including the `Accept-Encoding`,
  `User-Agent`, `Host` and `X-Forwarded-For` that MediaTailor requires from a CDN.
- **Frame-accurate cue points with ESAM** ([ADR-0017](docs/adr/0017-ssai-only-frame-accurate-cues-with-esam.md)).
  Cue points snap to the nearest frame (about ±17 ms at 29.97 fps) instead of the first tag's 2 s GOP grid. As in the
  first tag, a point between segment boundaries adds ESAM to that title's job only (`SccXml`, `ResponseSignalPreroll`
  0); on the MPEG-TS ladder it splits all three variants at that frame (verified at 13.480 s and 26.994 s) and the
  grid resumes. Such a job is billed on the Professional tier, about $0.06 instead of $0.0375 per content minute
  (`library___ingest` reports `pricing_tier`); job templates, kits and other titles stay on the Basic tier. Finalize
  keeps a cue point when a segment boundary of every variant lies within 100 ms of it.

### Fixed since the first v3.0.0 tag

- VOD source names are at most 50 characters (Channel Assembly's limit; 64 was assumed), so assets with ids longer
  than about 29 characters no longer fail registration.
- **Capacity guidance is now measured, not extrapolated**
  ([delivery-and-multi-cdn.md](docs/delivery-and-multi-cdn.md#capacity)). MediaTailor SSAI reuses Channel Assembly
  responses across sessions, and a load test of one channel up to 1,600 concurrent sessions leveled off near
  9 requests per second. The docs had said "about 300 concurrent viewers per channel"; that is replaced by the quota that
  actually binds, MediaTailor's Ad Insertion requests, at about 37,000 concurrent viewers per account at 70 % of default
  quotas. Updated: ADR-0007 and ADR-0017 consequences, the operations quota table, and the ad-insertion and
  Channel Assembly references.
- **The agent can create channels again.** `channels___create_channel` failed every time with
  `ForbiddenException: Cannot create the service-linked role`, and rolled back what it had created. MediaTailor's
  `ConfigureLogsForChannel` and `ConfigureLogsForPlaybackConfiguration` refuse a caller that may not create
  MediaTailor's service-linked role, even when `AWSServiceRoleForMediaTailor` exists, and the channels tool role
  could not. It now may create only that role (`iam:CreateServiceLinkedRole`, condition
  `iam:AWSServiceName = mediatailor.amazonaws.com`). IaC channels were not affected, because CloudFormation configures
  their logs with the deploying role. The end-to-end suite now creates and deletes an agent channel
  (`test_08_agent_channel`).

### Known issues

AWS service behaviour that Apple's mediastreamvalidator reports at Must level
([ad-insertion.md](docs/ad-insertion.md#monitoring-and-troubleshooting)):

- Channel Assembly removes a break's `EXT-X-DATERANGE` one playlist update too early: 552026 for most breaks on every
  live media playlist of the Channel Assembly origin and SSAI.
- MediaTailor SSAI writes the `EXT-X-PROGRAM-DATE-TIME` of the second and later ads in a pod one segment off in some
  reloads (536003).

## [2.0.0] - 2026-01-30

### Added
- 4K (3840x2160) encoding support with HEVC and AVC codecs
- 1080p (1920x1080) encoding support
- HEVC (H.265) codec support for improved compression at higher resolutions
- GitHub Actions CI/CD pipeline with lint, validate, build, and security scanning
- Dependabot configuration for automated dependency updates
- AWS X-Ray tracing for all Lambda functions
- HTTP/3 support for CloudFront distribution
- S3 Intelligent Tiering lifecycle policy for cost optimization
- KMS key rotation enabled for enhanced security
- SNS topic encryption with KMS

### Changed
- **BREAKING**: Upgraded Python runtime from 3.9 to 3.12
- **BREAKING**: Updated boto3 layer to 1.35.x (from 1.26.48)
- Updated crhelper to 2.0.11+ for improved CloudFormation custom resource handling
- Updated all Lambda functions with type hints and improved error handling
- Reduced IAM permissions to follow least-privilege principle
- Improved CloudFront security with TLS 1.2 minimum and HTTPS redirect
- Increased Lambda memory to 256MB for better cold start performance
- Increased Lambda timeouts for more reliable processing
- Added retry configuration with adaptive mode to AWS SDK clients
- Reduced segment duration from 8s to 6s for lower latency
- Enhanced S3 bucket security with encryption and public access blocks
- Improved logging with structured format

### Fixed
- Resource cleanup in custom resource delete handlers
- Error handling for duplicate VOD source creation
- Channel stop before delete to prevent deletion failures

### Security
- Restricted Secrets Manager policy from wildcard to GetSecretValue only
- Added specific resource ARNs to IAM policies instead of wildcards
- Enabled server-side encryption on all S3 buckets
- Added public access blocks to S3 buckets

### Removed
- Legacy Python 3.9 compatibility
- Overly permissive IAM wildcard policies

## [1.0.0] - 2023-02-01

### Added
- Initial Version

[3.0.0]: https://github.com/philhar88/AWS-Fast-Channels/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/philhar88/AWS-Fast-Channels/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/philhar88/AWS-Fast-Channels/releases/tag/v1.0.0
