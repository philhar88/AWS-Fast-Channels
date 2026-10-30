# Release Controller

The Release Controller is the release module (`modules.release`, off by default; see
[operations.md](operations.md#configuration-reference)). It is the only writer of what monetizes a channel: the
MediaTailor Monetization Functions and the sibling playback configurations that run them. It is being built on the
`feature/agentic-monetization` branch. This page covers the apply path, the drift check, the playback plan, the
ownership guard and the siblings' logs. The smoke gate, promotion and unattended rollback follow, then the
origin-secret rotation.

- [Siblings](#siblings)
- [The release bundle](#the-release-bundle)
- [The apply path](#the-apply-path)
- [The playback plan](#the-playback-plan)
- [Drift](#drift)
- [The ownership guard](#the-ownership-guard)
- [The siblings' logs](#the-siblings-logs)
- [Alarms](#alarms)
- [Deploy and destroy](#deploy-and-destroy)
- [Troubleshooting](#troubleshooting)

## Siblings

Each monetized channel (`modules.release.channels`) keeps its v3.0.0 playback configuration and gains siblings that
send their ad requests as a templated OpenRTB POST to Prebid Server:

| Sibling | Role |
|---|---|
| `<channel>-p-a`, `<channel>-p-b` | The pair. One is *stable* (viewers play it), the other *idle* (the next release goes to it) |
| `<channel>-p-z` | The probe (`modules.release.probe_sibling`): house ads only, for canaries and smoke tests. It publishes the PRE_SESSION hook's opt-in events, so the session controls it commits can be read in its logs |

These names, and the controller's IAM grants on `<channel>-p-*`, belong to the channel: the configuration refuses
an IaC channel named `<channel>-p-...` (with the module on or off, so turning it on cannot collide) and one named
`<prefix>-agent`, whose siblings would be agent-owned names.

The channel's policy admits the siblings by the static pattern `<channel>-p-?`, set when the channel is created
([channel-assembly.md](channel-assembly.md)). The Release Controller never changes a channel policy: a policy change
on a running channel is not session-safe.

## The release bundle

The bundle is the desired state: every Function and every sibling of the deployment
(`fast_channels.core.monetization`, schema `contracts/monetization-bundle.schema.json`). CDK renders it at synth and
validates it with every rule (R-F1 to R-S1, R-A1 and R-A2): Functions limits, the JSONata allow-list, the body
template, the cache keys, the channel-policy pattern and the rollout strategy. The deploy-time values (the channels'
Channel Assembly URLs, the content base URL) are CloudFormation tokens there. The bundle never holds the origin
secret or its name.

At deploy, the custom resource `Custom::DesiredState` validates the bundle again with those values resolved, stores
it as `release/<digest>/bundle.json` in the stack's bundle bucket and, when the digest is new, puts a
`Desired State Rendered` event on the stage's bus (`contracts/events.schema.json`). A bundle that breaks a rule fails
the deployment before anything is applied.

Function ids are `<p>_[<scope>_]<name>_rNNNN`. The bundle holds the stems; the release number is allocated when the
bundle is applied. An id is created once and never updated, so a release that changes a Function creates new ids.

## The apply path

The event starts the state machine `<prefix>-release` (Step Functions Standard, JSONata):

1. **Prepare** loads the bundle and runs every validator again, plus a check that the document holds neither origin
   secret. A bundle that fails is refused and ledgered (`release.reject` on the chain `release/bundle`). Otherwise
   the deployment gets its release number: an event delivered twice gets the same one (and is skipped once
   applied), a deployment that brings an earlier bundle back gets a new one.
2. **Accept** takes the controller's lock, so releases apply one at a time: a release that finds another one
   applying waits for it (up to 20 minutes). It then chooses the siblings the release configures (every sibling of
   a channel never released, else the idle sibling and any sibling not configured yet) and ledgers
   `release.accept`.
3. **Functions** creates the Function trees those siblings map, children first, with the ownership tags
   (`fast:owner=release-controller`, `fast:release=rNNNN`, `fast:change=<change id>`). An existing id with the same
   definition is left alone (a retried step); one with another definition stops the release.
4. **Configure** (one sibling at a time): `PutPlaybackConfiguration` with the origin secret read from Secrets Manager
   at that moment, its logging, and its vended-log deliveries ([below](#the-siblings-logs)).
5. **Publish** records each channel's siblings in the state table `<prefix>-release`, rewrites the playback plan,
   ledgers `release.apply` on the chain `release/<channel>` and gives the lock back.
6. **Check drift** runs the drift check once.

Any failure after Prepare marks the release `FAILED`, gives the lock back and ledgers `release.fail` with the error
(a lock left by an execution that died expires after the state machine's 90-minute timeout). The first release
makes `-p-a` stable and `-p-b` idle; later releases go to the idle sibling. The ledger's principal is
`release-controller`.

To apply a bundle again (a failed release, or a lost event), start the state machine with the event's detail as
input: `{"bundle_key": "release/<digest>/bundle.json", "digest": "<digest>", "correlation_id": "<id>"}`. The
`release.accept` record holds the bundle key, the digest and the `request`; the same id retries that release, a new
id makes a new one. Redeploying an unchanged stack does nothing: CloudFormation does not call the custom resource.

## The playback plan

`/plan/playback/<channel>.json` in the media bucket, served by the CDN with `Cache-Control: max-age=30`, names the
stable sibling and its playback URL per output:

```json
{
  "v": 1,
  "channel": "fastm-demo",
  "configuration": "fastm-demo-p-a",
  "release": "r0001",
  "playback": {"hls": "https://<cdn>/v1/master/<hash>/fastm-demo-p-a/hls.m3u8"},
  "updated": "2026-10-29T12:00:00Z"
}
```

Players that want the monetized stream read it; the v3.0.0 URLs keep working.

## Drift

Every hour, and after every release, the drift check renders each recorded sibling again from its release's bundle and
compares it with `GetPlaybackConfiguration`, its logging and `GetFunction` for each Function of its tree, and checks
each monetized channel's policy against rule R-C1. It logs each difference by path, never by value (a header holds
the origin secret), publishes their count as `ReleaseDrift` and repairs nothing: drift means someone outside the
controller changed what it owns. The alarm `<prefix>-release-drift` fires on any drift.

## The ownership guard

With the module on, every IAM role of the stage gets an explicit Deny (the managed policy `ReleaseGuard` in each stack)
of `PutPlaybackConfiguration`, `DeletePlaybackConfiguration`, `PutFunction`, `DeleteFunction`,
`CreatePrefetchSchedule`, `DeletePrefetchSchedule` and `PutChannelPolicy` on the siblings (`<channel>-p-*`), their
prefetch schedules, the monetized channels and the deployment's Functions (`<p>_*`), and the reading of the
siblings' log group ([below](#the-siblings-logs)). Only the controller's apply and reaper functions, and the readers of
that group, are exempt. The agent tools can still create and delete their own channels' configurations, but not
a monetized one. A break-glass operator acts outside the stage.

## The siblings' logs

Each sibling has one delivery source per vended-log type (`<sibling>-ads`, `-manifest`, `-transcode`). Every log type
goes to the log group `/aws/vendedlogs/mediatailor/<prefix>-release`; ad decision server and manifest logs also go to
the lake (`modules.ops`, [observability.md](observability.md#lake)). The deliveries leave out the fields that can
hold the origin secret or a viewer's ticket (`requestHeaders`, `rawAdsRequest`; `mediaTailorPath`, `input`, `http`;
`fast_channels.core.mediatailor_logs`).

The log group is encrypted with the module's own KMS key (rotated yearly). Its key policy delegates nothing to IAM:
the account administers the key, and CloudWatch Logs uses it for this log group only. The key protects the logs at
rest and lets them be shredded (disable the key and the group is unreadable), but it does not choose who reads them:
CloudWatch Logs decrypts for any caller that IAM lets read the group. So the release guard also denies reading the
group (`GetLogEvents`, `FilterLogEvents`, `StartQuery`, live tail, export, subscription filters) to every role of the
stage but its named readers (today the zero-ad-sessions detector). An account administrator outside the stage can
still read it.

The Pod Decision Service counts the siblings' impressions from this group: the subscription filter
`<prefix>-pds-delivery-release` sends their impression beacons to its delivery function, with the same pattern as its
filter on the stage's group ([pds.md](pds.md#reservations-and-delivery)). The Release stack owns the filter, because
it owns the group; the Pds stack publishes the function's ARN as `/<prefix>/pds/delivery-function`.

## Alarms

| Alarm | Fires on |
|---|---|
| `<prefix>-release-drift` | Any drift |
| `<prefix>-release-dead-letters` | A `Desired State Rendered` event that could not start the state machine |
| `<sibling>-presessioninithook-errors`, `-preadsrequesthook-errors`, `-function-errors`, `-getmanifest-errors` | Any error of the sibling in five minutes |
| `<prefix>-release-zero-ads-sessions` | Sessions on a sibling that saw three breaks without an ad request (hourly) |

Every alarm treats missing data as not breaching. The alerts go to the stage's alert topic.

## Deploy and destroy

`FastChannels-<prefix>-Release` needs the Prebid Server guidance's CloudFront domain, so `make deploy-demand`
deploys it after the Pds stack ([demand.md](demand.md#deploy-and-destroy)); `make deploy` leaves it alone. It reads
the Media stack's content base URL, CDN, media bucket and channel URLs from the SSM parameters
`/<prefix>/release/*`, which the Media stack writes when the module is on, so run `make deploy` first.

Deleting the stack (`make destroy-demand`, or `deploy-demand` with the module turned off) runs `Custom::ReleaseReaper`
first: it deletes the siblings with their deliveries and delivery sources, the Functions (executors first) and the
playback plans, each owner-checked by tag. A deployment that takes a channel out of `modules.release.channels`
deletes that channel's siblings and plan the same way (stop the channel first: its policy narrows); the Functions
stay until the stack goes.

## Troubleshooting

| Symptom | Look at |
|---|---|
| A deploy fails at `Custom::DesiredState` | The custom resource's log: the rules the bundle breaks, one per line |
| No release after a deploy | The state machine's executions; `<prefix>-release-dead-letters`; the ledger chain `release/bundle`. Start the state machine by hand ([above](#the-apply-path)) |
| An execution waits at Accept | Another release is applying; its execution holds the lock until it ends |
| `release.fail` in the ledger | Its `error`; an `ImmutableFunctionError` means a Function id exists with another definition (someone put it outside the controller) |
| `<prefix>-release-drift` in alarm | The drift function's log (`release drift`): the paths that differ |
