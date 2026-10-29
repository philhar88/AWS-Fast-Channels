# 0019 — MediaTailor Functions, the Release Controller, sibling configurations and a static channel-policy pattern

**Status:** Accepted

## Context

The monetization modules need MediaTailor to send a templated OpenRTB POST to Prebid Server, run per-session and
per-break logic (session controls, show-level metadata, later the ticket redeem) and change that logic safely while
viewers watch. MediaTailor Monetization Functions do the logic: `HTTP_REQUEST`, `CUSTOM_OUTPUT` and
`SEQUENTIAL_EXECUTOR` documents mapped on a playback configuration's hooks (`FunctionMapping`). Phase 0 measured what
changing them does to live sessions (P0-08):

- A FunctionMapping remap reaches live sessions at their next break in about 3 s, fleet-wide, with no client error.
  It is a good rollback and a bad canary.
- A channel-policy change on a running channel takes 3–4.5 minutes to grant, breaks 6–13 % of an allowed
  configuration's playlist requests for 5–6 minutes when it revokes, and a revocation under traffic is ineffective
  for more than 6 minutes.
- `PutFunction` does not enforce the documented limits or the JSONata function list; an id updated in place changes
  every configuration that maps it.

## Decision

- **CDK renders, the Release Controller applies.** `cdk synth` renders the desired state, a bundle of every
  Function document and every sibling configuration (`fast_channels.core.monetization`, schema
  `contracts/monetization-bundle.schema.json`), validated by rules R-F1 … R-S1, R-A1 and R-A2. A custom resource
  publishes it by digest and announces it on the stage's bus; the Release Controller (Step Functions Standard and
  Lambda) is the only writer of Functions, sibling configurations and their vended-log deliveries.
- **Immutable FunctionIds.** `<p>_[<scope>_]<name>_rNNNN`: a release creates new ids and never updates one. Each
  sibling keeps three trees (the current one, the last good one, the one before); older trees are deleted.
- **Siblings.** Each monetized channel keeps its v3.0.0 configuration and gains `<channel>-p-a` and `-p-b`, which
  swap stable and idle, and the house-only probe `-p-z`. A release is applied to the idle sibling only; promotion
  rewrites `/plan/playback/<channel>.json` and deploys the release pointer; rollback remaps the sibling to its last
  good tree.
- **The release pointer** is the AppConfig profile `release` in the stage's environment, which monitors the composite
  alarm `<prefix>-release-candidate`. A promotion deploys it gradually (the rollout tier's strategy, never all at
  once); a rollback, or a first release that moves no viewer, deploys it with `<prefix>-release-at-once` (100 % at once,
  no bake). `AppConfig.AllAtOnce` is not used: the monitors watch its 10-minute bake, so a rollback could itself be
  rolled back while the candidate alarm is still in ALARM, and it would hold the environment, which takes one
  deployment at a time, for 10 minutes (fastm, 2026-10-29).
- **Kill switches are never rolled back by a release alarm.** The `flags` profile shares the environment; while the
  environment is monitored it deploys with `<prefix>-flags-at-once` (no bake), which no monitor can roll back. The
  monitor role may call `cloudwatch:DescribeAlarms` on `*`: AppConfig finds a composite alarm only with an unfiltered
  call, and reports anything less as "monitors do not exist", rolling every deployment of the environment back.
- **A static channel-policy pattern.** A monetized channel's policy admits its v3.0.0 configuration and
  `<channel>-p-?`, set when the channel is created. The Release Controller never calls `PutChannelPolicy`.
- **Ownership guard.** Every other role of the stage is denied the controller's writes on its resources and the
  reading of the siblings' log group (managed policy `ReleaseGuard`).
- **The siblings' logs** go to one log group of the module, encrypted with its own KMS key, and to the lake, without
  the fields that may hold the origin secret or a ticket. The key protects them at rest and lets them be shredded; it
  does not choose their readers: CloudWatch Logs decrypts for any caller IAM lets read the group (observed on fastm,
  2026-10-29). Access is IAM's, so the guard denies reading the group to every role of the stage but its named readers
  (the zero-ad-sessions detector and the smoke test). This corrects the build plan's "decrypt limited to the Release
  Controller roles": no key policy limits who reads a log group through CloudWatch Logs.

## Consequences

- Turning the module on for an existing channel widens its policy once; the deployment refuses that while the
  channel runs.
- A Function change always goes through a release; a hand-made change is drift (`<prefix>-release-drift`).
- The bundle is the reviewable record of what monetizes a channel: `cdk diff` shows a change before it is released.
