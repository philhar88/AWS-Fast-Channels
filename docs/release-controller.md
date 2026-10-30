# Release Controller

The Release Controller is the release module (`modules.release`, off by default; see
[operations.md](operations.md#configuration-reference)). It is the only writer of what monetizes a channel: the
MediaTailor Monetization Functions and the sibling playback configurations that run them. It is being built on the
`feature/agentic-monetization` branch. This page covers the apply path, the smoke gate, promotion and unattended
rollback, the origin-secret rotation, the drift check, the playback plan, the ownership guard and the siblings' logs.

- [Siblings](#siblings)
- [The release bundle](#the-release-bundle)
- [The apply path](#the-apply-path)
- [Promotion and rollback](#promotion-and-rollback)
- [Origin-secret rotation](#origin-secret-rotation)
- [The playback plan](#the-playback-plan)
- [Prefetch](#prefetch)
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
template, the cache keys and scopes ([Function caches](#function-caches)), the channel-policy pattern and the rollout
strategy. The deploy-time values (the channels'
Channel Assembly URLs, the content base URL) are CloudFormation tokens there. The bundle never holds the origin
secret or its name.

At deploy, the custom resource `Custom::DesiredState` validates the bundle again with those values resolved, stores
it as `release/<digest>/bundle.json` in the stack's bundle bucket and, when the digest is new, puts a
`Desired State Rendered` event on the stage's bus (`contracts/events.schema.json`). A bundle that breaks a rule fails
the deployment before anything is applied.

Function ids are `<p>_[<scope>_]<name>_rNNNN`. The bundle holds the stems; the release number is allocated when the
bundle is applied. An id is created once and never updated, so a release that changes a Function creates new ids.

## Function caches

Every `HTTP_REQUEST` in the bundle declares a cache scope (`CacheScope`) and the reason for it
([ADR 0034](adr/0034-every-function-request-cached-by-scope.md)). Anything cacheable is cached, and its key names
exactly what makes an answer unique: too few values serve one session's or break's answer to another, too many only
spend upstream calls. Validator R-F8 holds the `Cache`, its key and its TTL to the scope:

| Scope | One answer per | The key names | `TtlMaximumSeconds` at most |
|---|---|---|---|
| `title` | title | `scte.segmentation_upid` (the content id) | 300 s |
| `break` | break of a channel | the channel as a `\|<channel>\|` segment, and `scte.event_id` or `scte.segmentation_event_id` | the shortest grid break (30 s) |
| `session` | session | `session.uuid` | 300 s |
| `session_break` | session and break | `session.uuid` and the break's event id | the shortest grid break (30 s) |
| `global` | playback configuration | nothing but the key's `'<name>\|v<N>\|'` prefix | 300 s |
| `none` | call: never cached (a single-use call such as a ticket redeem) | no `Cache` | — |

- A key reads its scope's identifiers and no other input, and never `player_params.*` or `avail.random` (R-F4).
- A `break` key names its channel because SCTE-35 event ids are unique per channel only. A break-scoped Function is
  therefore per sibling (`<p>_<sibling>_<name>`), not deployment-wide.
- Every TTL is 1 s at least. The cache cannot be invalidated: a change of meaning ships as a new `v<N>` in the key.
- The scope and the reason stay in the bundle. `PutFunction` never receives them, and drift compares only what it
  sends.

Cached answers are shared per playback configuration and split into 4–5 partitions, so the upstream of a `title`
key sees about 4–5 calls per title per configuration per TTL, and a `session_break` key one call per session and
break.

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

## Promotion and rollback

A channel's first release has no candidate: it is live once applied, and the pointer is published at once (it
moves no viewer). After a later release configured a channel's idle sibling, that sibling is the **candidate**.
The release keeps the controller's lock until it is promoted or rolled back, so the next one waits:

1. **Smoke test.** A probe session on the candidate for two breaks, judged on its MediaTailor logs three minutes
   later (their delivery delay): every ad marker led to an ad request, every break was filled, no Function or hook
   failed, the Pod Decision Service recorded a decision per break, and no PRE_ADS request
   [timed out](#timed-out-requests). When no
   monetized channel runs, the smoke runs on the lab channel `<prefix>-lab` (BASIC, created stopped): the controller
   schedules a block with a break a minute through the agent tools, starts the channel and stops it after. Without a
   running channel or the lab channel, the release stays applied and is not promoted (`release.hold`).
2. **Soak.** A new probe session for three breaks, judged the same way.
3. **Pointer.** A new version of the AppConfig profile `release` (application `<prefix>-fast`, environment
   `<prefix>`), `{"<channel>": "<candidate>"}`, deployed with the stage's strategy (`modules.release.strategy`;
   never all at once). The environment monitors `<prefix>-release-candidate`, a composite alarm over the pair
   siblings' hook and marker alarms: in ALARM, the deployment rolls back. The controller polls the deployment every
   minute and stops it when a candidate's PRE_ADS request [timed out](#timed-out-requests).
4. **Promotion** at 100 %: the candidate becomes stable, the plan names it, `release.promote` is ledgered, and the
   Function trees no sibling keeps are deleted.

A failed smoke or soak, or a rolled-back pointer, runs **R1**: the candidate is remapped to its last good tree
(a remap reaches live sessions at their next break in about 3 s), the pointer goes back to the stable siblings at
once, the plans name them, and `release.rollback` is ledgered. The same runbook runs when `<prefix>-release-candidate`
goes to ALARM outside a promotion: each pair sibling whose hook, Function or marker alarm is in ALARM goes back to its last good tree. `GetManifest.Errors` stops a pointer deployment but remaps nothing: a stopped channel or its origin raises it, not the sibling's Functions.

### Timed-out requests

An `HTTP_REQUEST` step that reaches its `RequestTimeoutMilliseconds` inside an executor is neither a Function error
nor a hook error: the step completes with a null response, the hook goes on, and no error event is logged (fastm
2026-10-30: a 1,000 ms request to an endpoint that answers in 2,500 ms). So no error alarm sees a dependency that is
down or slow. Only the step's `Function.Latency` shows it, at its timeout.

The smoke judge and the pointer poll read `AWS/MediaTailor` `Function.Latency` (Maximum, per minute) of each
`HTTP_REQUEST` step in the candidate's PRE_ADS tree. The dimensions are the sibling, the step's FunctionId in the
release, `PRE_ADS_REQUEST` and `HTTP_REQUEST`. A step at or above its own request timeout (from the release's bundle)
in two minutes or more has timed out: the smoke fails, or the pointer deployment is stopped and R1 runs.
`release.rollback`'s reason names the requests (`timed_out`).

The window starts with the probe session, or with the deployment. One slow minute is a cold call, not a broken
release. On a healthy smoke, the crawl lookup reached its 250 ms once (266 ms), on the session's first break.

This is a runtime check, not an alarm: the FunctionId changes with every release, and each release's bundle sets its
own timeouts.

## Origin-secret rotation

The siblings send one of the two origin secrets of the stage in `X-Fast-Origin`; the WAF rule group
`<prefix>-demand-origin` (rule `fast-origin-required`) admits either, so a rotation never blocks a POST. The state
machine `<prefix>-release-rotation` rotates every `modules.release.secret_rotation_days` days (90 by default).
EventBridge Scheduler starts it daily, and a scheduled run that is not due ends at once: Scheduler fires a rate
schedule when the schedule is created, so a rate of 90 days would rotate on every deploy (fastm, 2026-10-30). Due
means that the days have passed since the siblings last switched secret, or, before the first rotation, since the
current secret last changed. Start the state machine by hand on any suspected exposure; a run by hand always
rotates:

```bash
aws stepfunctions start-execution --state-machine-arn <RotationStateMachineArn> --input '{"reason": "exposure"}'
```

1. **Begin.** It holds the Release Controller's lock, as its own execution, so no release or other rotation runs
   meanwhile (it waits for one that holds the lock). It chooses the slot the siblings do not send.
2. **Admit.** That slot gets a new value (Secrets Manager), which the rule group admits beside the current one.
3. **Probe.** After a minute, a probe POST with the new value through the guidance's CloudFront must not be blocked;
   it is retried until the change has reached CloudFront.
4. **Switch.** Every configured sibling sends the new value: `PutPlaybackConfiguration` of its own release, the same
   call as a remap, so live sessions use it at their next break. The slot is recorded.
5. **Verify.** While a monetized channel is on air, the siblings' ad requests since the switch must have been
   answered (no `ERROR_ADS_INVALID_RESPONSE`) and a break filled; it is retried for one break.
6. **Retire.** The old slot gets a value nobody has seen, which the rule group admits instead of the old one. The lock
   is given back.

A failure is ledgered (`rotation.fail` on the chain `release/rotation`), gives the lock back and raises
`<prefix>-release-rotation-failed`. Every value a sibling may send is still admitted at that point, so the rotation
can simply run again. No secret value is logged or ledgered. The rotation assumes that everyone who could read the
ADS logs before the switch knows the old value (MediaTailor logs the request headers; the module's log group leaves
them out).

The rule group's values are changed by the rotation, not by CloudFormation: a later update of the DemandEdge stack
that changes the rule group resolves the secrets again, and so writes the same values.

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

## Prefetch

With `modules.release.prefetch.enabled` (on by default with the module), MediaTailor asks for each break's ads before
the break, for every session that requests a playlist in the break's retrieval window, and spreads those requests
over a window ([ADR 0033](adr/0033-prefetch-single-schedules.md)). Two things change at the break: the ad request
herd moves off the marker, and creatives MediaTailor has never seen are prepared before they air instead of being
skipped at first sight.

The prefetcher (`<prefix>`'s `PrefetchFunction` output) runs every 5 minutes, after every `Schedule Published` and on
every `Census Changed`. For each monetized channel that is running:

1. It reads the schedule of record: `GetChannelSchedule` for the next 6 hours, and `DescribeProgram` for each program
   with breaks, whose TIME_SIGNALs carry the break's SCTE-35 event id and MPU UPID. A break's length is snapped to the
   stage's break lengths (the default ladder's slate grid).
2. For each sibling the controller recorded whose letter is in `modules.release.prefetch.siblings` (the pairs `a`/`b`
   and `k`/`l`; never the probe `z`), it lists the sibling's prefetch schedules (`ScheduleType=ALL`).
3. It deletes the schedules of breaks that left the schedule or moved, then creates one SINGLE schedule for each of
   the next `horizon_breaks` breaks that has none.

Each schedule, `pf-<event id>`, tagged `fast:owner=release-controller`:

| Part | Setting |
|---|---|
| Retrieval | From `retrieval_lead_seconds` (150) to `retrieval_end_seconds` (30) before the break, spread over `shaping_window_seconds` (90) with `RETRIEVAL_WINDOW` traffic shaping |
| Dynamic variables | `scte.event_id`, `scte.segmentation_upid` (the content id), `session.avail_duration_ms` (rule R-S1) and `session.avail_duration_secs` (the POST body's stored request) |
| Consumption | From 20 s before the break to 10 s after it, only into the break with that `scte.event_id` |

- **Create-only.** A schedule is never changed: a moved break's schedule is deleted and created again, and a re-planned
  break's is deleted, because retrieval transcodes bill whether or not the break airs. MediaTailor removes expired
  schedules after at least 7 days; they do not count against its quota of 25 active schedules per configuration.
- **Never beside RECURRING.** A sibling with a RECURRING schedule gets no SINGLE one; `<prefix>-release-prefetch`
  fires.
- **Left to real-time insertion:** a break without an MPU UPID, a break whose length is not a break length of the
  stage, a break too close to retrieve (its retrieval would stay open less than 30 s), and a channel that is not
  running.
- **On retrieval** the sibling's Functions run as at a break (PRE_ADS's content lookup included), and Prebid Server and
  the Pod Decision Service answer minutes ahead. MediaTailor rounds the duration to whole seconds (`60000`); the PDS
  checks the pod against the break of record ([pds.md](pds.md)). Reservations are taken at retrieval and delivered by
  our tracker's impression; one whose break the session never watched is released by the sweeper after 2 hours.
- **Joins** use our tracker (`d=<sid>.<evid>`): prefetch events carry no `adBreakCorrelationId`, and a
  prefetch-consumed `FILLED_AVAIL` names the session's previous request URL.
- A session that joins after a break's retrieval window gets real-time insertion for that break.
- Siblings never carry YO (`YieldOptimizationConfiguration`), which MediaTailor does not support with prefetch.
- The reaper deletes every schedule of a sibling before the sibling.

Measured on a throwaway stage on 2026-10-30. It aired 8 breaks of 60.06 s with 40 SSAI sessions on each general
sibling, on Prebid Server 4.4.0 (`tests/e2e/test_16_prefetch.py`):

| | When the ad requests arrived, relative to the break | p05–p95 width |
|---|---|---|
| Real time (no schedule) | +13 s to +18 s (the sessions' playlist polls) | 4.1 s |
| Prefetch without traffic shaping | 150 s to 146 s ahead | 3.4–3.9 s |
| Prefetch, 90 s `RETRIEVAL_WINDOW` | 150 s to 67 s ahead | 59–72 s (14 breaks) |

- Every prefetched avail was filled (600 of 600, 60.06 s each), with no creative skipped at first sight.
- At the first break, MediaTailor saw the demand's creatives for the first time, through a retrieval, and filled the
  break fully.
- On the same stage, a real-time first sight of the house ads, on the channel's v3.0.0 configuration, skipped both
  (`NEW_CREATIVE`) and filled 0 of 60.06 s.
- The PDS answered every retrieval with the session id, event id and UPID. Our tracker credited the line ads reserved
  at retrieval when they aired.

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
siblings' log group ([below](#the-siblings-logs)). Only the controller's apply, reaper and prefetcher functions, and
the readers of that group, are exempt. The agent tools can still create and delete their own channels' configurations,
but not a monetized one. A break-glass operator acts outside the stage.

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
| `<prefix>-release-log-loss` | MediaTailor made ad requests its vended ADS log does not have (hourly, over an hour that ended 20 minutes before): `AWS/MediaTailor` `AdDecisionServer.Ads` sample counts against the log's `MAKING_ADS_REQUEST` per sibling. MediaTailor can leave a requested, filled break out of the log entirely, and the silent-loss detector, which counts logged markers, does not see it |
| `<sibling>-markers-without-ads-request` | In 2 of 10 minutes with at least 5 markers, more than half of the markers had no ad request: an invalid rendered body or an exhausted PRE_ADS budget makes no request and logs no error, and the break plays unfilled. A break's marker counts once (MediaTailor finds a Channel Assembly marker again when it gains its duration, without a request), and a ratio leaves out the viewers who join during a break. `<prefix>-lab-x` has this alarm too, for a harness to prove it (`tests/e2e`) |
| `<prefix>-release-rotation-failed` | An origin-secret rotation failed: the ledger's `rotation.fail` says at which step; run it again |
| `<prefix>-release-candidate` | Any of the above (but drift) on a pair sibling: rolls a pointer deployment back (AppConfig monitor) and runs R1 |
| `<prefix>-release-prefetch` | A prefetcher run that failed on a sibling, or a sibling with a RECURRING schedule, in 15 minutes |

Every alarm treats missing data as not breaching. The alerts go to the stage's alert topic.

## Deploy and destroy

`FastChannels-<prefix>-Release` needs the Prebid Server guidance's CloudFront domain, so `make deploy-demand`
deploys it after the Pds stack ([demand.md](demand.md#deploy-and-destroy)); `make deploy` leaves it alone. It reads
the Media stack's content base URL, CDN, media bucket and channel URLs from the SSM parameters
`/<prefix>/release/*`, which the Media stack writes when the module is on, so run `make deploy` first.

Deleting the stack (`make destroy-demand`, or `deploy-demand` with the module turned off) runs `Custom::ReleaseReaper`
first: it deletes the siblings with their prefetch schedules, deliveries and delivery sources, the Functions
(executors first) and the
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
| `release.hold` in the ledger | No monetized channel ran and there is no lab channel (or no agent to start it): start a channel and run the release again by hand |
| `release.rollback` in the ledger | Its `reason` (a failed smoke verdict lists what failed; `ROLLED_BACK` names the alarm, or `timed_out` the PRE_ADS requests that reached their timeout) |
| `<prefix>-release-log-loss` in alarm | **Log loss means delivery is under-counted.** The Pod Decision Service counts deliveries from the same log's `BEACON_FIRED`, so the breaks MediaTailor left out may have delivered impressions that were not counted, and the sweeper may release their reservations. The reconciliation function's log names the siblings and the hour. Reconcile delivery from the trackers' own logs (the impression URLs the creatives call); the alarm detects and never back-fills |
