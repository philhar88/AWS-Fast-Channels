# Known limitations and product suggestions

FAST Channels runs on AWS services and open-source projects. While we built and tested it in us-east-1, from
October to November 2026, we found behaviour in them that a deployment should know about: defects, undocumented
behaviour, limits, and features that would make the reference simpler. This page lists each item, what it means for
a deployment, what this repository does about it, and what we suggest to the product.

- **Ids.** Each item keeps the id that the code's `OPTIMIZATION` markers and [Workarounds to remove](workarounds.md)
  use:
  - `W-2 <n>`: AWS Elemental MediaTailor and the AWS services around it.
  - `W-7`: Amazon Bedrock and Bedrock AgentCore.
  - `W-8`: the Prebid Sales Agent (A) and Prebid Server Java (C).
  - `W-10`: the Guidance for Advertising Agents on AWS.
  - `W-11`: the Guidance for Deploying a Prebid Server on AWS.
  - `W-12`: a Prebid Server Java race.

  An item without an id shows "—".
- **Reporting.** We record these items here, not as support cases or upstream issues. A row that says "Reported to
  AWS: no (owner's decision)" or "Reported upstream: no (owner's decision)" is one the maintainer may still report.
- **What this repo does.** A link to the workaround, the code or the page that handles the item. "Nothing needed"
  means that the behaviour does not affect the reference. "Not used" means that the reference does not use the
  feature.
- **Evidence.** The measurements come from development stages, on the dates given. A service can change its
  behaviour without notice, so check an item again before you rely on it.

`tests/e2e/test_workarounds.py` checks that every id in [workarounds.md](workarounds.md) has a row on this page.

- [AWS Elemental MediaTailor](#aws-elemental-mediatailor)
- [Channel Assembly](#channel-assembly)
- [AWS AppConfig](#aws-appconfig)
- [Amazon CloudWatch Logs](#amazon-cloudwatch-logs)
- [AWS KMS](#aws-kms)
- [Amazon API Gateway](#amazon-api-gateway)
- [AWS CloudFormation and AWS Glue](#aws-cloudformation-and-aws-glue)
- [AWS Budgets](#aws-budgets)
- [Amazon Bedrock and Bedrock AgentCore](#amazon-bedrock-and-bedrock-agentcore)
- [Other AWS services](#other-aws-services)
- [The AWS Prebid Server guidance](#the-aws-prebid-server-guidance)
- [The AWS advertising agents guidance](#the-aws-advertising-agents-guidance)
- [Prebid Server Java](#prebid-server-java)
- [The Prebid Sales Agent](#the-prebid-sales-agent)
- [Other open-source dependencies](#other-open-source-dependencies)
- [Product suggestions](#product-suggestions)
- [Observed behaviour](#observed-behaviour)

## AWS Elemental MediaTailor

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 1.1` | `NEW_CREATIVE` skips continue after a creative is ready. 3 of 24 new sessions skipped a preconditioned HLS creative 7 to 44 s after other sessions on the same configuration had filled with it (2026-10-27). | A viewer who joins at a break can lose ads that are ready. | Prefetch prepares the creatives before the break airs ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Explain the "transcode variant set" key, and fill new sessions with a ready creative. See FR-2. |
| `W-2 1.2` | Vended `TRANSCODE_LOGS` are incomplete. At least 125 import and transcode jobs gave 14 unpaired events and no `PRECONDITIONED_WORKFLOW_SUCCESS`, and `cacheStatus` is null on almost every event. | The logs cannot show when a creative is ready. | Nothing needed: prefetch prepares the creatives, and nothing reads readiness from the logs. | Deliver every transcode event, and document the `cacheStatus` values. |
| `W-2 1.6` | `Live/VodMaximumAdsPersonalizationTimeMilliseconds` is a wall clock from the manifest request's first event, so it includes PRE_ADS and the hooks of earlier breaks. The documentation counts only ADS activity. With a 1,000 ms maximum, a 600 ms PRE_ADS request and a 600 ms ADS timed out 21 times out of 21. | A PRE_ADS hook shortens the time the ADS gets, and the break can lose its request. | Rule R-P3 sets the maximum to at least the PRE_ADS executor's timeout plus the ADS timeout ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Count only ADS activity, as documented, or correct the documentation. Say if hook time also reduces the Yield Optimization remainder. |
| `W-2 1.7` | A break whose budget is spent before the ad request plays unfilled. In one test, PRE_ADS took 1,549 ms against a 1,000 ms maximum. MediaTailor sent no request and counted no `AdDecisionServer.Timeouts`. Its only event was `WARNING_NO_ADVERTISEMENTS`. | The break loses its revenue, and no metric shows why. | R-P3 keeps the budget above the hook's timeout (W-2 1.6). The silent-loss detector alarms on breaks that made no ad request (W-2 1.23). | Add a metric or a log event for "ADS request not sent: budget exhausted". |
| `W-2 1.8` | On VOD, POST_ADS runs outside the shared 2,000 ms hook budget, and parallel PRE_ADS overspends it. One test playlist took 7.8 s. | A VOD playlist with several breaks can be slow. | Not used: the reference serves live channels only. | Keep both inside the budget, or document the behaviour. |
| `W-2 1.9` | PRE_MANIFEST silently drops the first injection of a creative URL. Later injections are transcoded despite `StreamingMediaFileConditioning=NONE`: 15 × 2.002 s segments instead of the creative's own 5 × 6.006 s. | An injected creative loses its first break and then plays as a transcode. | Rule R-P4 forbids PRE_MANIFEST functions ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Inject on first use and honour `NONE`, or document both. |
| `W-2 1.10` | Sessions with `aws.logMode=DEBUG` write DEBUG events into the account-wide legacy group `MediaTailor/ManifestService`, with retention "never", even when only vended logs are on. A player can ask for DEBUG whenever no function overrides it. | Viewer requests can reach a log group that keeps them forever. | The PRE_SESSION function writes `aws.logMode` `DISABLED` for monetized sessions (`SESSION_CONTROLS` in `core/.../monetization/functions.py`). With the identity module, the edge drops player-set `aws.*` (W-2 FR-1). A direct caller can still ask for DEBUG, so set a retention on the legacy group if it exists. | Write DEBUG events only to the configured destinations, or document the behaviour. |
| `W-2 1.11` | The ADS log's `adBreakCorrelationId` is undocumented and inconsistent. `AD_MARKER_FOUND` is logged twice per Channel Assembly break: the second time when the `DATERANGE` gets its `DURATION`, with a new id and no request. A prefetched break's events carry `prefetchScheduleName` instead of the id. | Counting markers overcounts breaks, and joins on the id miss prefetched breaks. | Our queries key breaks on `coalesce(adBreakCorrelationId, prefetchScheduleName)` and count only the first marker ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Define the id, and document the second marker and `prefetchScheduleName`. |
| `W-2 1.12` | A SINGLE prefetch rounds `session.avail_duration_ms` to whole seconds: 60060 reaches the ADS as 60000. | A length check on a prefetched break gets the wrong value. | The schedule also sends `session.avail_duration_secs`, which the body uses ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Pass the value as set, or document the rounding. |
| `W-2 1.14` | A prefetched `FILLED_AVAIL` can name another request's `adsRequestUrl`. A late joiner's fill named the session's previous real-time request. A 120 s break filled from its retrieval named the previous 30 s break's URL. Its `prefetchScheduleName` was right in both cases. | A join of fills to breaks by URL files a fill under the wrong break. | Nothing needed: the lake's break views and the e2e harness take a prefetched event's break from `prefetchScheduleName` first ([observability.md](observability.md#lake)). | Make each per-avail field name the avail's own request. |
| `W-2 1.15` | A session that starts while only the last segment of a finished break is in its window gets an ad request and a `FILLED_AVAIL` for a 6.06 s "avail". | An ad that fits such an avail would be billed for a break that is over. | Nothing needed: no ad of the reference's 30 s grid fits such an avail. | Do not personalize a break that has ended, or document the behaviour. |
| `W-2 1.16` | `PutFunction` with a non-empty `Tags` map fails on an existing function with `BadRequestException: Invalid tagrisInternalId.`. The same call without tags succeeds and keeps the tags. | Automation that sends a function's tags on every update fails. CloudFormation updates of `AWS::MediaTailor::Function` may fail the same way (not tested). | An existing function is compared without its tags ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Accept the tags on an update. |
| `W-2 1.17` | A Function wrapper hop that the hook timeout stops is logged as `ERROR_UNKNOWN`, not as a timeout. | A slow wrapper is harder to diagnose. | Nothing needed. | Log it as a timeout. |
| `W-2 1.18` | The Functions engine swallows JSONata errors. `{% $number('not-a-number') %}` in a `CUSTOM_OUTPUT` completes with `SUCCESS` and no `Function.Errors`, where JSONata 1.x and 2.x raise D3030. | A broken expression fails silently, and local JSONata tests cannot prove that the engine behaves the same way. | Nothing in code: the release smoke test judges real breaks before a promotion ([release-controller.md](release-controller.md)). | Document the engine's error semantics, and count its errors. See FR-4. |
| `W-2 1.19` | The PRE_MANIFEST hook's input labels a streaming HLS creative `"mimeType":"video/mp4","delivery":"progressive"`. | A PRE_MANIFEST function cannot tell streaming creatives from progressive ones. | Not used: R-P4 forbids PRE_MANIFEST functions. | Label the media files as they are. |
| `W-2 1.20` | `PutFunction` rejects a hyphen in `FunctionId`, but the pattern is not published, and the AWS recipe library uses hyphenated ids. | An id copied from the recipes fails at deployment. | Function ids use the prefix with `-` replaced by `_` ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Publish the pattern in the API reference, botocore and CloudFormation, and fix the recipes. |
| `W-2 1.21` | DASH ad segment redirects (`/v1/dashsegment`) ignore the creative MPD's `BaseURL`, at MPD and at Period level (2026-10-16, not checked since). | A DASH creative must keep its segments beside its MPD. | Not used: the standard ladder is HLS only ([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)). | Honour `BaseURL`, or document the behaviour. |
| `W-2 1.22` | MediaTailor does not JSON-escape body-template values, although the documentation says it does with `Content-Type: application/json`. The value `xAy` reached the ADS unescaped, so the ADS decoded it as `xAy`, and a double quote or a lone backslash made the body invalid JSON. | A player value with a quote or a backslash loses the break (W-2 1.23). | The POST body holds only MediaTailor's own variables, each inside a JSON string. Player values travel in headers (R-P1, [workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Escape the values as documented. |
| `W-2 1.23` | A rendered body that is not JSON skips the ad request silently. MediaTailor logs the marker and nothing else: no request, no error, no `WARNING_NO_ADVERTISEMENTS`. `PutPlaybackConfiguration` also stores a template that is not JSON, without a warning. | A direct caller can force ad-free breaks, and no metric counts them. | R-P1 checks the template. The silent-loss detector alarms on markers without an ad request, and the release smoke test fails a break that made none ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Validate the template in `PutPlaybackConfiguration`, and log and count a skipped request. |
| `W-2 1.24` | `MAKING_ADS_REQUEST.requestHeaders` lists `X-Device-User-Agent`, but MediaTailor does not send it: it was absent on 9 of 9 POSTs at the ADS's edge and on 85 of 85 at Prebid Server. MediaTailor sends the viewer's user agent as `User-Agent`, and sends no `X-Device-IP` (W-2 3.1). | The log is wrong, and an ADS that expects the VAST 4.x headers (§1.1.3) finds neither. | Nothing needed: the PDS takes the device type from `User-Agent` and the viewer's address from the `X-Forwarded-For` chain ([pds.md](pds.md#the-viewer-ip-rule)). | Send the VAST 4.x device headers, or correct the log. |
| `W-2 1.25` | The default ADS log stores request-header values in plain text, among them the origin secret and the playback ticket. `RAW_ADS_REQUEST` also logs the body, which holds `device.ip`. | Anyone who reads the ADS log gets the secrets. | The log deliveries select fields without `requestHeaders` and `rawAdsRequest`, and the events that log the ticket are never on for a ticketed sibling ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | A redaction list or a "secret" flag for headers. See FR-9. |
| `W-2 1.26` | `PutFunction` enforces neither the documented 1,000-character limits nor the JSONata allow-list. Expressions of 1,001 and 1,500 characters ran, and `$pad`, which is not on the allow-list, failed only at run time (`RESTRICTION_ERROR`), and the hook failed open. | A function that relies on the leniency breaks silently if enforcement arrives. | Rules R-F1 and R-F2 check the limits and the allow-list before any `PutFunction` ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Enforce both in `PutFunction`, or correct the user guide. |
| `W-2 1.27` | MediaTailor drops an ad without `<Impression>` from a pod, with no skip reason. With no `<Impression>` in the response, the whole response fails as `ERROR_ADS_RESPONSE_PARSE`. | A third-party pod can lose ads, and the log does not say why. | Nothing needed: every ad that the fast-ctv module renders carries our impression tracker ([demand.md](demand.md)). | Log a skip reason such as `MISSING_IMPRESSION`. |
| `W-2 1.28` | A PRE_SESSION `aws.logMode=DISABLED` does not cover the session's initial request. A direct `?aws.logMode=DEBUG` init still wrote `SESSION_INITIALIZED` (with the full query string), `ORIGIN_MANIFEST` and `GENERATED_MANIFEST`, to the vended log and to the legacy group (W-2 1.10). | A session's first request can be logged, with its query string, whatever the function says. | Nothing in code: the PRE_SESSION value covers the rest of the session. With the identity module, the edge drops every parameter of the init request but the playback ticket (W-2 FR-1). | Apply the value to the whole session, as the hook page says. |
| `W-2 1.29` | The OpenAds recipes in AWS's sample monetization functions (v1.0.0) read `seatbid[0].bid[0].ext.prebid.cache.bids.cacheId`. In its collated-VAST mode, the public OpenAds server leaves `seatbid` empty, so the recipes' outputs would be null, with no error. We read this from the source and did not run it. | A deployment built from those recipes may never fill. | Not used. | Say which response shape the public server returns, and fix the recipes. |
| `W-2 1.30` | `ConfigureLogsForChannel` and `ConfigureLogsForPlaybackConfiguration` call `iam:CreateServiceLinkedRole` with the caller's credentials on every call, even when the role exists, and fail without the permission. The AWS managed `AWSElementalMediaTailorFullAccess` policy cannot configure logs. | Least-privilege automation cannot configure MediaTailor logs. | The roles that configure logs may create MediaTailor's service-linked role, on its exact ARN ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Check for the role first, or document the permission for both APIs. |
| `W-2 1.32`, `W-2 1.13` | The vended ADS log can leave out events of a break that MediaTailor requested and filled. On 2026-10-29 a whole break was missing. On 2026-11-06 one session of seven lacked a prefetch retrieval, a `FILLED_AVAIL` and `INITIALIZED`. Earlier, 1 of 67 prefetch retrievals was missing (1.13). `Avail.FilledDuration` counted each break. | Log-based monitoring and delivery miss real breaks and impressions. | An hourly reconciliation against `Avail.FilledDuration` alarms on missing breaks. Delivery also counts our tracker's requests in the CDN's logs, and the e2e checks tolerate known gaps ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). See also [Impressions that only the CDN's logs count](#impressions-that-only-the-cdns-logs-count). | Find the cause, and deliver every event. |
| `W-2 1.33` | A session can retrieve a prefetched break as its partner sessions do, meet the break's marker inside the consumption window, and then neither fill from the retrieval nor make a real-time request, with no error or warning event. This happened in 3 of 33 sessions (2026-11-06): the markers came 35 to 39 s into the window, within 0.7 s of a partner that was filled. The CDN's logs show that those sessions stitched nothing. | The viewer gets no ads for the break, and no AWS metric shows it. | The zero-ads detector counts such breaks (`UnservedBreaks`) and alarms, and the e2e checks tolerate them in at most one session in five ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). Reported to AWS: no (owner's decision). | Find the cause, fall back to a real-time request, and log an event when a retrieval is not consumed. |
| `W-2 1.34` | When a session meets a prefetched break's start marker again (the same `DATERANGE` ID and `START-DATE`, no `DURATION`), MediaTailor can fill the break again in real time, mid-break. The viewer's pod restarted with its first ad. | The ADS is asked twice for one break, and the viewer sees the pod start again. | Delivery credits a repeated impression once (`repeat`), and the zero-ads detector counts refilled breaks (`RefilledBreaks`, no alarm) ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly), [#151](https://github.com/philhar88/AWS-Fast-Channels/pull/151)). | Fill a break once per session. |
| `W-2 2.16` | Without `ScheduleType`, `ListPrefetchSchedules` returned `{"Items": []}` while a RECURRING schedule existed. The default is not documented. | Automation that uses the default misses RECURRING schedules. | Every listing passes `ScheduleType=ALL` ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Document the default, or make it `ALL`. |
| `W-2 2.20` | `session.avail_duration_secs` rounds to the nearest second, halves up (30.49 → 30, 30.5 → 31), and a zero or missing `CUE-OUT` duration arrives as 300 s. Neither is documented. | An ADS cannot tell a break without a duration from a 300 s break. | 0 s and 300 s are never grid durations: the module answers them with an empty pod, and the slate plays ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Document the rounding and the default. |
| `W-2 2.22` | `session.client_ip` is the leftmost `X-Forwarded-For` entry, which the client controls, even through CloudFront. The documentation does not say which entry it uses. | A client can replace its own address in ad requests. | The viewer's address comes from the chain that each hop appends, read at Prebid Server ([pds.md](pds.md#the-viewer-ip-rule), [workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Use a trusted entry, and document which one. |
| `W-2 2.28` | An `HTTP_REQUEST` step that times out inside a `SEQUENTIAL_EXECUTOR` counts in no error metric: `Function.Errors` and `PreAdsRequestHook.Errors` stayed 0 while `Function.Latency` reached the step's timeout. | A slow or dead dependency is invisible to error alarms. | The smoke judge and the pointer poll compare each PRE_ADS step's `Function.Latency` with its timeout ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Count step timeouts, or document what `Function.Errors` counts for executor steps. |
| `W-2 2.32` | IAM authorizes `ListPrefetchSchedules` on `prefetchSchedule/*`, not on the playback configuration that the Service Authorization Reference names. | A least-privilege policy written from the reference fails. | The list grant is account-wide for prefetch schedules ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Correct the reference for `ListPrefetchSchedules` and `CreatePrefetchSchedule`. |
| `W-2 3.8` | Service Quotas shows no maximum and no `UsageMetric` for MediaTailor's request quotas (`L-32FBC51C` 10,000/s, `L-B9D43015` 400/s, `L-C13EFDBD` 50/s). `L-32FBC51C` limited first, at about 0.1875 requests/s per viewer, so default quotas allow about 53,000 concurrent viewers per account. | No Service Quotas alarm can warn before the quota limits playback. | A Metrics Insights sum of every configuration's `Requests`, compared with the default quota ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | See FR-10. |
| `W-2 FR-1` | MediaTailor endpoints are public. When PRE_SESSION fails, the session keeps the player's `aws.*` values. No setting makes MediaTailor ignore player-set `aws.*`. | A direct caller can force ad-free sessions, or DEBUG logging (W-2 1.10). | An hourly zero-ad-sessions detector and lake view. With the identity module, the CDN's edge rebuild drops every init parameter but the playback ticket. A direct caller still reaches MediaTailor ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | See FR-1. |

### Impressions that only the CDN's logs count

The CDN's logs make up for W-2 1.32, at a cost. Since the reservation hold became 10 minutes
([ADR-0022](adr/0022-delivery-states-billing-and-the-hash-chained-ledger.md), amendment of 2026-11-06), a line ad that
only the CDN's logs count is credited late:

- the delivery function reads a CDN log file 10 to 25 minutes after the firing, past the hold, so the sweeper has
  already released the reservation;
- the impression is still counted once, as `late`, with a `money.deliver` ledger event marked `late: true`, and its
  price is taken from the line's budget shard again, even past the line's budget;
- each such ad can overspend its line by at most one ad's price;
- the metric `LateDeliveries` counts them. With the ADS log complete, we have seen no late credit (0 in 14 days).

Details are in [pds.md](pds.md#reservations-and-delivery) ("Late impressions") and in ADR-0036,
[Reservation holds](adr/0036-pds-sidecar-in-the-prebid-server-task.md#reservation-holds).

## Channel Assembly

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 1.3` | A channel-policy change on a running channel breaks sessions of configurations that it keeps allowed. 92 of 720 polls (12.8 %) got HTTP 504 "Unable to obtain template playlist" over 6 minutes, and new grants took 3 to 4.5 minutes. | A release that edits the policy disturbs viewers. | One static sibling pattern, `<channel>-p-?`, set when the channel is created. Releases never call `PutChannelPolicy` ([workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | State the propagation time, and leave the grants that a change keeps undisturbed. |
| `W-2 1.4` | Revocation under traffic takes more than 6 minutes. A configuration removed from the policy still got service on about 60 % of its requests 6 minutes later, while it had live sessions. On a quiet channel, the same revocation took effect at once. | A revoked configuration keeps serving while it has viewers. | Releases never revoke by policy (W-2 1.3). A sibling is retired by deleting its configuration. | Make revocation independent of the revoked configuration's traffic. |
| `W-2 1.5` | The channel policy does not keep an unlisted configuration of the same account off the channel while listed configurations have traffic. Unlisted siblings got service on 55 % and 68 % of probes, and on 0 % once the listed ones went quiet. `aws:SourceAccount` held throughout, so the exposure stays within one account. | The policy is not a boundary between the configurations of one account. | A configuration is retired by deleting it, not by removing it from the policy. Reported to AWS: no (owner's decision). | Enforce `aws:SourceArn` per configuration, and document any shared origin fetch. |
| `W-2 1.31` | The as-run log can leave out the `AdBreakStart` of a break that went on air up to about a minute before `StopChannel`. A session saw ads in a break that the channel stopped 25 s into, and the log never had its start. Stops 3.5 s after a break's start lost the event, and a stop 20 s after the start kept it. On 2026-11-06, a stop 5.5 s into a break lost it again. As-run events arrive in batches 13 to 50 s after their time. | A reconciliation from the as-run log marks such a break as not aired. | Before the sweep marks a break `UNAIRED`, it reads the channel's `StopChannel` calls from CloudTrail's event history. A break that went on air at most 60 s before a stop is marked `AIRED`, with `stopped_at`. A test stage showed this on 2026-11-06 ([channel-assembly.md](channel-assembly.md#the-schedule-of-record-and-the-epg), [workaround](workarounds.md#aws-elemental-mediatailor-and-channel-assembly)). | Log every break that aired, or document the loss. |

## AWS AppConfig

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.6` | In the AppConfig Agent Lambda extension 2.0.27899, `Configuration-Version` does not always match the content served. During and after entity-based gradual deployments we saw content and header pairs (b, 1), (b, 3) and (c, 2). After an alarm rollback, a cold-started extension served v2 with version 1. | A reader that trusts the header records the wrong version. | Whatever reads the pointer or the flags logs a hash of the content (`content_version`), never the header ([workaround](workarounds.md#other-aws-services), [#151](https://github.com/philhar88/AWS-Fast-Channels/pull/151)). | Fix the header. |
| `W-2 4.7` | A 0-minute deployment ignores its monitors. `AppConfig.AllAtOnce` bakes for 10 minutes under them, and no other deployment of the environment can start meanwhile. A monitor in `INSUFFICIENT_DATA` rolls a deployment back. The pages say none of this. | A kill switch or a rollback can wait 10 minutes, or be rolled back itself. | Our own LINEAR 100 %, 0-minute, no-bake strategy for rollbacks, first pointers and flags. R-A1 never deploys a promotion all at once, and R-A2 wants `notBreaching` monitors ([workaround](workarounds.md#other-aws-services)). | Document when monitors apply, and correct the rollback text for `INSUFFICIENT_DATA`. |
| `W-2 4.8` | The Resource Groups Tagging API keeps the tags that `StartDeployment` put on deployments after the application is deleted, and `UntagResource` returns `ResourceNotFoundException`. Six entries remained at the re-check. | A tag-based inventory lists resources that are gone. | Nothing in code. Before you count a tagged resource as residue, describe it. | Remove the tags with the application, or document the behaviour. |
| `W-2 4.13` | AppConfig reports a denied `DescribeAlarms` call as a composite alarm that does not exist ("The following CloudWatch monitors do not exist"), and rolls every deployment back. A role allowed `DescribeAlarms` on the alarm's ARN alone cannot monitor a composite alarm. | A least-privilege monitor role stops every deployment of the environment. | The monitor role may call `cloudwatch:DescribeAlarms` on `*` ([workaround](workarounds.md#other-aws-services)). | Report the denial, and document that a composite monitor needs `DescribeAlarms` on `*`. |
| `W-2 4.14` | AppConfig authorizes `CreateHostedConfigurationVersion` and `GetDeployment` on the application's ARN, and `GetDeployment` on the environment's ARN too. | A policy that names only the documented resources is denied. | The promote grants also name the application's and the environment's ARNs ([workaround](workarounds.md#other-aws-services)). | Document the resources that each action checks. |

## Amazon CloudWatch Logs

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| — | A log group takes at most 5 subscription filters (`L-87E7D306`, not adjustable). Channel Assembly writes every channel's as-run events into one account-wide group, `MediaTailor/Channel/AsRunLog`. | At most 5 deployments with their own as-run filter fit in an account and Region. | The account's shared as-run router takes one filter for every `shared` deployment ([modules.md](modules.md#account-level-limits-and-teardown-residue), [ADR-0032](adr/0032-shared-as-run-router.md)). | A per-channel as-run destination, or native events (FR-7). |

## AWS KMS

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.12` | A customer managed key on a log group does not gate its readers. An IAM principal that the key policy does not allow `kms:Decrypt` read the group (`GetLogEvents`, `FilterLogEvents`, events in plain text) and wrote to it (`PutLogEvents`). The CloudWatch Logs encryption guide says that such a caller needs KMS permissions. The key's logs-service grant cannot be narrowed to force decryption by the caller. | The key protects the data at rest, but only IAM decides who reads it. ADS logs can hold secrets (W-2 1.25). | An explicit IAM deny of the log reads on the siblings' log group, for every role of the stage but its readers ([workaround](workarounds.md#other-aws-services)). Reported to AWS: no (owner's decision). | Make the behaviour match the guide, or correct the guide. |

## Amazon API Gateway

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.9` | An HTTP API reports HTTP/2 requests as HTTP/1.1, in `$context.protocol` and in the Lambda event's `requestContext.http.protocol`. CloudFront's `cs-protocol-version` was right. | Access logs and functions see the wrong protocol. | Nothing needed. | Report the protocol of the request. |
| `W-2 4.10` | Behind CloudFront, a REST API's `$context.identity.sourceIp` is the viewer's address (the `X-Forwarded-For` entry left of CloudFront's address), not the TCP peer that the reference names. | A rule or a log that expects the TCP peer gets the viewer. | Nothing needed: the identity API's access log holds no address ([identity.md](identity.md#logs)). | Correct the reference, or the value. |
| — | When the account's API Gateway `cloudwatchRoleArn` names a role that no longer exists, a REST API stage with access logging deploys and serves, and drops its logs without any signal. With no role set, the same stage update is refused. | A REST API can run without its access log, and nothing reports it. | The identity stack does not take the account-wide setting over. [identity.md](identity.md#logs) says how to check and set it, and the e2e test skips its log check, saying why, when the role is not valid. | Check that the role exists and can be assumed when a stage turns logging on, or report failed deliveries. |

## AWS CloudFormation and AWS Glue

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.15` | `AWS::Glue::Database` cannot change `DatabaseInput.Name` in place, although the reference says "No interruption". The update looks the database up by its new name and fails with `NotFound`, because the resource's primary identifier, `DatabaseName`, is create-only. | A rename rolls the stack back. | Overlay O1 names the guidance's database with the stage prefix ([modules.md](modules.md#account-level-limits-and-teardown-residue)). | Replace the resource on a name change, or rename it as documented. |
| `W-2 4.5` | `AWS::Evidently::Project` is still LIVE in the CloudFormation registry, although Evidently shut down on 2025-10-17. | A template can still declare a service that is gone. | Not used: feature flags are AWS AppConfig ([observability.md](observability.md#runbooks-flags-and-the-probe)). | Deprecate the type. |

FR-5 asks for CloudFormation support of MediaTailor's API-only settings.

## AWS Budgets

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.11` | AWS Budgets actions reject daily budgets ("AWS Budgets Actions don't support daily granularity budget for now"), and the user guide does not say so. | A budget action cannot enforce a daily cap. | A monthly budget at 31 times the daily cap. The token counter enforces the day ([workaround](workarounds.md#other-aws-services), [ADR-0029](adr/0029-per-task-model-registry.md)). | Support daily budgets in actions, or document the limit. |

## Amazon Bedrock and Bedrock AgentCore

The model registry calls Bedrock Converse through an application inference profile per task. The AgentCore Gateway
inference route is documented as an alternative and is not built ([ADR-0029](adr/0029-per-task-model-registry.md)),
so most gateway items below do not affect the reference. "Profile" means an application inference profile.

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.1` | Bedrock reports Nova Canvas and Nova Reel as end of life (`GetFoundationModel`: "reached the end of its life"), but the Nova user guide lists them as available. | Code that picks a model from the guide fails at run time. | Not used. The advertising agents guidance's image generator uses Nova Canvas (W-10 item 5). | Update the guide. |
| `W-7 D1` | The gateway's Messages route answers HTTP 200 with an error body (`UnknownOperationException`) for an unknown model and for a profile, and a profile cannot be invoked through it. InvokeModel answers 400 for the unknown id. | A client that trusts the status code misses errors. | Not used: the registry calls Converse directly. | Return a 4xx in the client's dialect, and make profiles invocable. |
| `W-7 D2` | An MCP gateway accepts and serves an inference target, although the API reference says that an MCP gateway can have only MCP targets. | It is unclear whether one gateway may hold both kinds of target. | Not used. | Say whether one gateway can hold both. |
| `W-7 D3` | A rate limit keyed on `qualifiedModelId` resolves after a rule's model pin, without the target prefix, although the documentation says that limits apply before rules. | Limits per alias are not possible. | Not used. Limits would key on the task's role. | Align the behaviour and the documentation. |
| `W-7 D4a` | Under 8 minutes of sustained load, a limit of 5,000 tokens a minute admitted about 20,300 tokens a minute (117 of 120 requests). | A token limit cannot act as a cost circuit breaker. | Not used. Each task's daily cap is enforced by our own token counter ([ADR-0029](adr/0029-per-task-model-registry.md)). | Enforce the documented rate. |
| `W-7 D4b` | The pages disagree on whether token limits apply to InvokeModel. They do: a 429 "Token rate limit exceeded". | Readers cannot tell which page is right. | Not used. | Make the pages agree. |
| `W-7 D4c` | The token estimate seems to ignore `system` content (inferred). At a 15,000 a minute limit, requests with a system block of about 32,600 tokens and a 7-token message were admitted, and `retryAfter` implied an estimate of about 7 tokens. | Limits cannot be sized for prompts with a cached prefix. | Not used. | Document what the estimate counts. |
| `W-7 D5` | Target creation sends `GET {endpoint}/v1/models` even with explicit models, so an endpoint that answers 429 there fails the target. The gateway also rejects `providerPath` on templated paths. | An endpoint without a model list cannot be a target. | Not used. | Document both behaviours. |
| `W-7 D6` | Output tokens burn down quota at 10 times their count for Claude Haiku 5.5 and Sonnet 5.5 (`EstimatedTPMQuotaUsage` = input + 10 × output), and the burndown page lists neither model (its default is 1:1). | Quotas sized at 1:1 are 10 times too small for output. | Nothing in code. Size token quotas with 10 × output. | Add both models to the burndown page. |
| `W-7 D7` | The connector does not discover profiles: `/inference/v1/models` lists 323 ids and no profile ARN, and an unqualified profile ARN gives 404. | A profile needs target qualification. | Not used. | Discover profiles, as documented. |
| `W-7 Q1` | CloudFormation's `AWS::BedrockAgentCore::GatewayRule` has no `matchModels`, model pin or `fallback`, which all work through the API. `GatewayRateLimit` lacks the request header and body dimensions that botocore has. | A fallback rule needs a custom resource. | Not used. | Bring CloudFormation and the CDK L1 constructs to parity with the API. |
| `W-7 Q2` | The `CfnGateway` schema defaults `ProtocolType` to MCP, but the handler does not apply the default: a gateway created without it is non-MCP. | An L1 inference gateway relies on undocumented behaviour. | Not used: the agent tools' gateway is an MCP gateway. | Align the schema and the documentation with the handler. |
| `W-7 Q3` | The gateway does not serve Converse (`400 Unsupported inference path`). | A Converse client cannot move onto the gateway. | Not used: the registry stays on Converse. | Serve Converse. |
| `W-7 Q5` | Bedrock model invocation logging is set per account and Region only. Gateway inference logs carry routing metadata only: no bodies, no principal, no token counts. | Body logging for one task also logs every other task's prompts, the recommender's viewer data among them. | The production checklist says to restrict who reads the invocation logs ([operations.md](operations.md#production-hardening)). | Logging scoped by task, profile or principal. |
| `W-7 Q6` | The gateway's 403 bodies carry its execution role's session ARN and the Bedrock resource ARN. | Behind JWT inbound authorization, external callers can see the ARNs. | Not used: the inference route is not built. | Say whether this is intended, or map the errors. |
| `W-7 Q8` | AgentCore Runtime's examples use A2A 0.3, and support for A2A 1.0 on Runtime and on gateway passthrough is not stated. The AWS reference buyer's 0.3 client failed against an A2A 1.0 seller with `-32601 Method not found`. | An A2A 1.0 agent on Runtime relies on undocumented support. | The buyer runs the guidance's fixed A2A 1.0 client on AgentCore Runtime ([selling.md](selling.md)). | State A2A 1.0 support, and when. |
| `W-7 Q10` | IAM authorizes `CreateAgentRuntime`, and the implicit `DEFAULT` endpoint, on `runtime/*`, because the runtime id does not exist yet. A name pattern cannot scope it. | A bounded deployment role must scope runtime creation by tag. | Nothing in code: CloudFormation creates the runtimes with the deploying role. A bounded role needs `aws:RequestTag` conditions. | Document how to scope runtime creation by name or tag. |

## Other AWS services

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-2 4.2` | The Bedrock Data Automation pages contradict each other on video custom output, which is documented, in the API enums and announced, and the IAB section is out of date: "For Preview", no taxonomy version, 24 of the 37 Content Taxonomy 3.1 Tier-1 categories. | A reader cannot tell what video custom output and the IAB categories support. | Not used: show-level metadata comes from the catalog ([content-enrichment.md](content-enrichment.md)). | Correct both pages. |
| `W-2 4.3` | Amazon Personalize documents a Next-Best-Action recipe, but `DescribeRecipe` rejects `aws-next-best-action` and `ListRecipes` has none. | A design built on the recipe cannot be deployed. | Not used. | Fix the API or the page. |
| `W-2 4.4` | AWS Elemental MediaLive Workflow Monitor alarms cannot deploy from CloudFormation: `AWS::MediaLive::SignalMap`'s `MonitorDeployment` is read-only. Their targets include neither Channel Assembly channels nor MediaConvert. | Baseline alarms of a Channel Assembly pipeline cannot live in infrastructure as code. | Not used: the reference's own alarms cover the pipeline ([observability.md](observability.md)). | See FR-6. |

## The AWS Prebid Server guidance

The [Guidance for Deploying a Prebid Server on AWS](https://github.com/aws-solutions-library-samples/prebid-server-deployment-on-aws)
v1.4.0, which the demand module deploys with overlays ([demand.md](demand.md#the-guidance-and-its-overlays)).

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-11 item 1` | With `EnableLogAnalytics=true`, the analytics reporter logs every request header verbatim, on Prebid Server Java 3.43.0 and 4.4.0. Our contract test found the origin secret and the playback ticket in its auction event. | Anyone who reads the analytics logs gets the secrets. | Stages deploy with `EnableLogAnalytics=false` ([workaround](workarounds.md#the-prebid-server-guidance-and-open-source-dependencies)). Reported upstream: no (owner's decision). | Omit or redact request headers. |
| `W-11 item 2` | `deploy.sh --container-image` runs `cdk deploy` without `--no-previous-parameters`, so CloudFormation keeps the previous `ContainerImage`. The update completes, and the tasks keep the old image. | An image update silently changes nothing. | Every deploy passes `--no-previous-parameters` and each parameter that it uses ([workaround](workarounds.md#the-prebid-server-guidance-and-open-source-dependencies)). | Pass `--no-previous-parameters`, or the `ContainerImage` parameter. |
| `W-11 item 3` | The stack, web ACL, load balancer and Glue database names are hard-coded. A second deployment fails with `Database already exists`. | One deployment fits per account and Region. | Overlay O1 prefixes the stack and Glue database names with the stage prefix. | Derive the names from a lowercased stack name or a parameter. |
| `W-11 item 4` | The synthesizer pins the bootstrap qualifier `hnb659fds` and has no asset-bucket setting. After a stack rename, `--parameters` keyed by the old stack name stop applying, without a warning. | A prefixed deployment on its own bootstrap needs a patch. | Overlay O1 gives each stage a dedicated bootstrap. | Make both configurable. |
| `W-11 item 5` | The web ACL has no extension point for customer rules. | A customer rule group needs a patch. | Overlay O2 references the stage's rule group first. | A parameter such as `ExtraWafRuleGroupArn`. |
| `W-11 item 6` | The web ACL's delete step uses the lock token from creation, so after any later web ACL update, stack deletion fails. We read this from the source and did not trigger it. | A web ACL changed after creation blocks the teardown. | Nothing needed: the origin secret rotates inside the referenced rule group, so the web ACL itself never changes (overlay O2). | Delete with the current lock token. |
| `W-11 item 7` | The guidance keeps `server.http.server-instances: 1` on 2-vCPU tasks. On 3.43.0, every sampled auction (3,999 of 3,999) ran on one event loop. With 2 loops, the p99 of a break herd after 150 s idle fell from 1.3–1.4 s to 378 ms. | Half of each task's vCPU is idle for requests. | `modules.demand.server_instances` defaults to 2 ([operations.md](operations.md#configuration-reference)). With more than one loop, the warm-up also covers W-12. | Make the default follow the vCPU count. |
| `W-11 item 8` | The scaling assumptions omit JIT warm-up. Fresh tasks timed out on 45 % of a 2,400-call herd 3 minutes after start, and were clean 14 minutes later. The health check (60 s interval, threshold 5) is slow to show it. | A new task can fail a break herd. | The census gate's warm-up replays requests before the first break ([demand.md](demand.md#scaling-the-census-gate-dayparts-and-warm-up)). | Document a warm-up before peak traffic, and a faster health check. |
| `W-11 item 9` | The simulator's first deploy with `EnableRtbFabric=true`, the default, fails without the RTB Fabric service-linked role. The rollback cannot delete the non-empty demo bucket, and the stack ends in `ROLLBACK_FAILED`. | A first simulator deploy with RTB Fabric can need manual cleanup. | Not used: the deploy passes `EnableRtbFabric=false` (`scripts/_demand.py`), so no RTB Fabric resource is created and no role is needed. With the guidance's default, the role must exist before the first deploy. | Name the role as a prerequisite. |
| `W-11 item 10` | The simulator's creatives (5, 15 and 30 s progressive 360×360 MP4s, all of one advertiser) cannot fill a CTV pod. | A pod demonstration needs other creatives. | Overlay O5 makes the simulator bid the stage's demo creatives ([demand.md](demand.md#demo-demand-and-the-bidder-simulator)). | Ship pod-ready creatives. |
| `W-11 item 11` | The main stack and the simulator each need a VPC with two NAT gateways. | A demand stage takes 2 VPCs and 4 Elastic IP addresses, against default quotas of 5 each per Region. | `modules.demand.simulator: lambda` runs the simulator as a Lambda function in the Prebid Server VPC ([modules.md](modules.md#account-level-limits-and-teardown-residue)). | State this in the prerequisites. |
| `W-11 item 12` | Stack deletion leaves resources behind: ten retained buckets (one with Object Lock), about 18 log groups and the KMS keys. | Each teardown needs manual cleanup. | `make destroy-demand` deletes what the templates retain and schedules the keys for deletion. The keys are its only residue ([demand.md](demand.md#deploy-and-destroy)). | Document a full teardown. |
| — | Each guidance stack creates two custom CloudFront cache policies, and an account allows 20 by default, shared with everything else in it. | Only a few demand stages fit in an account. | Count the free policies before deploying ([modules.md](modules.md#account-level-limits-and-teardown-residue)). | Share the policies between stacks, or use managed ones. |

## The AWS advertising agents guidance

The [Guidance for Advertising Agents on AWS](https://github.com/aws-solutions-library-samples/guidance-for-advertising-agents-on-aws),
whose reference buyer and governance agent the selling module deploys ([selling.md](selling.md#workarounds-to-remove)).

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-10 item 1` | The reference buyer's `create_media_buy` requires an approved `check_governance`, even with no governance agent, and refuses with `no_prior_check`. | Booking without governance is not possible. | The governance agent is deployed with the buyer. | Honour the "not governed" path, or document governance as mandatory. |
| `W-10 item 2` | Session recording is not best-effort: without `SESSIONS_TABLE_NAME`, every A2A turn fails with `-32603`. | The buyer needs a sessions table. | The module creates `<prefix>-buyer-sessions`. | Make session recording best-effort. |
| `W-10 item 3` | `deploy_all.py` escapes a permissions boundary in four ways: a prefix pattern (`^[a-z][a-z0-9]{0,19}$`) that has no room for a delimiter, the default CDK bootstrap roles (AdministratorAccess by default), unprefixed starter-toolkit resources, and no caller tags. | A boundary on the deploying principal does not contain the deployment. | The stage deploys the agents with its own CDK. | Accept a bootstrap qualifier, a delimited prefix, role boundaries and caller tags, or document the IAM needed. |
| `W-10 item 4` | The agents are not published as pinned images, or as CDK or CloudFormation constructs. | Adopters cannot deploy them from their own infrastructure as code. | The stage builds the pinned, unmodified upstream code into images. | Publish pinned, versioned images. |
| `W-10 item 5` | `image_generator.py` hardcodes `amazon.nova-canvas-v1:0`, which reached end of life on 2026-09-30 (W-2 4.1). | The creative path calls a retired model. | Not used: the stage deploys the buyer and governance agents only. | Replace the model id. |
| `W-10 item 6` | The reference buyer has no creative tasks, and its `create_media_buy` carries no creative. | Every buy that it books stays `pending_creatives` on the seller. | Booked buys serve the ladder kit's demo creatives, and the docs say so ([selling.md](selling.md#creatives)). | Add `sync_creatives` and creative assignment, or document that the buyer books without creatives. |
| `W-10 item 7` | The buyer and the governance agent must name their own runtime URLs in their environment, and the URLs exist only after `CreateAgentRuntime`. | A CloudFormation or CDK deployment cannot set them. | Stable `.invalid` identity URLs that name the stage and are never called (`core/.../selling/names.py`). | Take the identity from the runtime context, or use an id that is not a URL. |
| `W-10 item 8` | The buyer reads seller tokens and governance client secrets only from its environment. | Anyone allowed `bedrock-agentcore:GetAgentRuntime` can read them. | Nothing in code: restrict `bedrock-agentcore:GetAgentRuntime` (`infra/.../selling/agents.py`). | Resolve them from AWS Secrets Manager or AgentCore Identity. |

## Prebid Server Java

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-12` | `Metrics` and 15 nested metric classes fill plain maps lazily with `computeIfAbsent`, and every Vert.x event loop shares them. Since JDK 9, that throws `ConcurrentModificationException` when two loops first create the same key at once, and the auction fails with HTTP 500. Seen on 3.43.0, 4.4.0 and `master`. In a repro, 84 % to 94 % of two-thread trials threw. A warm server still races on a key that its traffic has not created yet, such as an adapter's request type. | The first concurrent auctions of a new task, or of a new adapter, account or request type, can fail, and the break plays without demand. | The warm-up sends the warm stored request first, one at a time. It answers every configured adapter from a stored bid response, so a new task creates the keys without calling a partner ([workaround](workarounds.md#the-prebid-server-guidance-and-open-source-dependencies)). Reported upstream: no (owner's decision). | Use `ConcurrentHashMap` in the `metric` package, or create the per-key objects at start. |
| `W-8 C2` | Stored bid responses (`imp.ext.prebid.storedbidresponse`) work only on single-imp requests. With 2 imps, Prebid Server calls the adapter over HTTP, without a warning, and the docs state no limit (`ExchangeService`, 3.43.0 and 4.4.0). | A structured pod cannot take its bids from stored responses. | The warm stored request is a pod of one slot (`warm_stored_request` in `core/.../pbs_config.py`). | State the limit, and warn in the response. |
| `W-8 C3` | A 3.43.0 configuration does not start on 4.4.0 without two settings: `gdpr.vendorlist.live.startup-cache-dir`, and `meta-info.vendor-id` on a configured `generic` alias, without which startup fails with a NullPointerException. | An upgrade fails at startup. | The rendered configuration sets both (`core/.../pbs_config.py`). | List both in the 4.x upgrade notes, and raise a named configuration error for the alias. |
| `W-8 C4` | Two values accept only enum names. An alias's `ortb-version: "2.6"`, as core bidder YAMLs spell it, stops startup. Activity Controls' `component_type: ["bidder"]`, as the Prebid docs spell it, makes the account unreadable. `ORTB_2_6` and `BIDDER` work. Seen on 3.43.0, and 4.4.0's `ComponentType` still has no `bidder` spelling. | A configuration written from the docs does not load. | The rendered configuration uses the enum names. | Accept both spellings, or give the enum names in the docs. |

**Version-bound, now moot:** two items applied to 3.43.0 only, and the reference runs 4.4.0 since v4.0.0. 3.43.0
dropped five OpenRTB 2.6 content fields (`genres`, `gtax`, `realtime`, `firstbroadcast`, `data[].cids`) without a
warning (`W-8 C1`), and 4.4.0 passes them. Under sustained load, 3.43.0 returned sporadic HTTP 400s that it did not
log, 19 in about 94,000 calls (`W-8 C5`, not verified).

## The Prebid Sales Agent

The [Prebid Sales Agent](https://github.com/prebid/salesagent) (`f92de9b2`, AdCP 3.1.1), the selling module's
seller ([selling.md](selling.md#workarounds-to-remove)).

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `W-8 A1` | `collection.kind: "channel"` fails the AdCP 3.1.1 schema, and validates only against 3.2.x. | A 3.1.1 seller cannot list a FAST channel as a channel. | Each product names its channel's breaks and genres in its description and placement (`core/.../selling/products.py`). | Move the seller to AdCP 3.2.x. |
| `W-8 A2` | The seller does not verify the buyer's `governance_context`: no JWS verification, no `jti` replay check, no `plan_hash` binding. | The buyer's governance token means nothing at the seller. | Nothing: the token is not verified at the seller yet ([selling.md](selling.md)). | Verify governance at the seller. |
| `W-8 A3` | The seller does not store or return collections, installments or `video_placement_types`. `get_products` omits them, and `Product(collections=…)` raises `TypeError`. | Linear airings cannot load as installments. | As for A1. | Store and return them. |
| `W-8 A4` | Bedrock enablement fails twice: the lock has no `boto3`, and the AI checks require `aws_access_key_id`, so an ECS task role is not enough. The ranking path also ignores the tenant's `ai_config`. | The seller's AI features need static keys on AWS. | Not used: the stage leaves the seller's AI features off. | Add the Bedrock extra, and accept the default credential chain. |
| `W-8 A5` | The signing keys live in Postgres, encrypted with a key-encryption key, and nothing reads `ADCP_SIGNING_PROVIDER="kms"`. | RFC 9421 signing keys cannot live in KMS. | The key-encryption key derives from a Secrets Manager seed (`infra/.../selling/seller.py`). | A pluggable KMS signer. |
| `W-8 A6` | No per-operation switch makes a signature mandatory: `required_for` cannot make a caller with a bearer token sign. | Signing cannot be required for spend operations. | Nothing: the buyer uses a bearer token, and the RFC 9421 posture is advertised but not required ([selling.md](selling.md#what-it-does-not-do)). | A per-operation setting that rejects other credentials. |
| `W-8 A7` | Single-tenant mode blocks `create_media_buy` until SSO is set up (`CONFIGURATION_ERROR`, `missing_tasks: ["sso_configuration"]`). | Booking needs multi-tenant mode. | `ADCP_MULTI_TENANT=true` ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Document this in the AWS deployment guide. |
| `W-8 A8` | Behind an AWS load balancer, which sets `X-Forwarded-Proto` to `http`, the seller rebuilds `@target-uri` as `http://…`, so RFC 9421 verification fails behind TLS termination. | Signed requests fail behind a load balancer. | CloudFront sends `Fly-Forwarded-Proto: https`, which the image's nginx prefers ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Document this in the AWS deployment guide. |
| `W-8 A9` | Testing mode still calls the public reference creative agent (`creative.adcontextprotocol.org`) for `sync_creatives`. | A deployment that must not call third parties needs a switch. | `CREATIVE_AGENT_URL` names a host that cannot resolve ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Say if this is intended, and document a switch. |
| `W-8 A10` | Products that property tags authorize publish the tenant's host, not the publisher's domain, and the seller re-mints `pricing_option_id` (`fast-ch1-cpm` comes back as `cpm_usd_fixed`). A product needs exactly one of `properties`, `property_ids` or `property_tags`. | Buyers see the wrong publisher and pricing ids. | The bridge names each product's properties ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Publish the publisher's domain, and keep the pricing option ids. |
| `W-8 A10 cancel` | `update_media_buy` with `canceled: true` answers `completed` and cancels nothing. `get_media_buys` still shows `pending_creatives`, with `cancel` among the valid actions, and a later `paused: true` fails with `INVALID_STATE`. | A buyer's cancel does not stop delivery. | Nothing: the bridge serves what the seller's database holds, so the line keeps serving until its flight ends. | Map `canceled` to the cancel action. |
| `W-8 A10 listing` | Without `status_filter`, `get_media_buys` lists `active` buys only, so a new `pending_creatives` buy is visible only by its id. | A buyer cannot find its own new buys. | Nothing needed: the bridge reads the seller's database. | List every buy, or document the default filter. |
| `W-8 A10 budgets` | A package's budget is kept only in the media buy's `raw_request.packages[]`. `media_packages.budget` and `package_config.budget` are null. | Readers of the package rows find no budget. | The bridge takes each package's budget from the stored request (`packages_of` in `vendor/.../fast/bridge.py`). | Store the budget on the package. |
| `W-8 A10 delivery` | `get_media_buy_delivery` keys `by_package` by its own fallback id `pkg_<product>_<n>`, not the booked package id, so per-package numbers split the totals evenly. The mock adapter also reports package `…_0` for the booked `…_1`, a rate of 12.0 against a $14 CPM, and spend with no impressions. | A buy with several packages shows its total split evenly. | Delivery is written to the mock adapter's seeded rows; the per-package split stays wrong ([selling.md](selling.md#delivery-back-to-the-buyer)). | Key delivery by the booked package id. |
| `W-8 A10 A2A` | Over A2A, integers come back as floats (`adcp.major_versions` `[3.0]`, `media_buy_count` `1.0`), where MCP returns integers, and the agent card declares no `securitySchemes`, although non-discovery skills need a bearer token. | A2A clients get wrong types, and cannot learn the authentication. | Nothing needed. | Restore integers on A2A, and declare the security schemes. |
| `W-8 A10 tools` | `tools/list` is 6.99 MB for 14 tools (`update_media_buy` alone is 4.18 MB), 54 times AdCP 3.2's 128 KiB target for MCP. | The seller is a poor target for generic MCP clients and gateways. | Nothing needed: the buyer calls the seller over A2A. | Shrink the tool schemas. |
| `W-8 A11` | The seller has no ad server adapter for MediaTailor. | Buys cannot reach MediaTailor without glue. | The bridge reads buys from the seller's database and writes delivery back ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Take a `mediatailor_pds` adapter upstream. |
| `W-8 A12` | The seller does not percent-decode the password in `DATABASE_URL`, and exits at boot when it cannot log in, with no retry. A correctly encoded `^` (`%5E`) failed every time. | A generated password that needs encoding stops the seller from starting. | The database password uses letters, digits and `-._` only ([workaround](workarounds.md#agentic-selling-the-advertising-agents-guidance-and-the-prebid-sales-agent)). | Unquote the URL's user and password. |

## Other open-source dependencies

| Id | Limitation | What it means for a deployment | What this repo does | Product suggestion |
|---|---|---|---|---|
| `FINDINGS M13-02` | Strands Agents derives prompt caching and the tool-result status from the model id, so an application inference profile ARN silently turns both off. | An agent on a profile loses prompt caching. | The agent sets both from the profile's model ([workaround](workarounds.md#the-prebid-server-guidance-and-open-source-dependencies)). | Resolve the profile's model, or take both settings explicitly. |
| — | AWS CDK's log-retention provider runs once more when the Operator stack is deleted, and recreates two log groups that no stack owns, one without retention. | `make destroy` leaves two log groups behind. | Delete them by hand after the destroy ([modules.md](modules.md#account-level-limits-and-teardown-residue)). | Do not recreate log groups on deletion. |
| — | AWS CDK's `BucketDeployment` fails when its CloudFront invalidation takes longer than it waits ("Unable to confirm that cache invalidation was successful", aws-cdk issue 15891). One invalidation took 18 minutes. | `make destroy` can fail once on the Operator stack. | Nothing in code. Wait until the invalidation completes, then run `make destroy` again. | Wait longer, or do not fail the deletion on a slow invalidation. |
| — | The IAB Tech Lab's CTV genre mapping misspells id 244 ("Parnormal Phenomenons"; the taxonomy names it "Paranormal Phenomena"). The Taxonomies repository has no LICENSE file, and its README states CC BY 3.0. | A copy of the mapping repeats the typo. | The reference uses the taxonomy's own label ([ATTRIBUTION.md](../core/src/fast_channels/core/crawl/taxonomy/ATTRIBUTION.md)). | Fix the label, and add a LICENSE file. |

## Product suggestions

Features that would let the reference delete code or close a gap. The ids are those of the code's markers.

- **FR-1: authenticated session initialization for MediaTailor.** Any one of these would do: a configuration setting
  that ignores player-set `aws.*`, a PRE_SESSION hook that fails closed, or origin or session authentication (an
  origin access control for MediaTailor, or signed session initialization). The zero-ad-sessions detector and its
  lake view would go, and with an `aws.*` lock the edge rebuild would keep only its ticket rule.
- **FR-2: one transcode per creative.** Concurrent first sights of a creative each start a transcode and skip the
  ad: four sessions started four jobs for one creative. Coalescing them per creative and variant set would remove a
  reason for prefetch (W-2 1.1).
- **FR-3: a creative-readiness API** to register creatives, pre-transcode them per variant set and query their
  state, for creatives that are not prefetched.
- **FR-4: a Functions test API and immutable `FunctionMapping` versions,** so that a release can test its functions
  against the real engine before the switch. In-place `PutFunction` updates left stragglers for up to 11.5 s, while
  remaps reached live sessions at their next break in about 3 s. Local JSONata tests cannot prove parity (W-2 1.18).
- **FR-5: CloudFormation support for API-only settings:** Function `Cache`, `YieldOptimizationConfiguration` and the
  PRE_SESSION manifest-log opt-in event types, which today need custom resources.
- **FR-6: Workflow Monitor targets for Channel Assembly and MediaConvert,** deployable from CloudFormation (W-2 4.4).
- **FR-7: native EventBridge events for MediaTailor:** fill-rate drops, function errors and channel state. Today the
  registry has only schemas derived from CloudTrail, so operators poll `ListAlerts`. A channel-state event would also
  replace the as-run sweep's CloudTrail lookup (W-2 1.31).
- **FR-8: a warning or a metric when a streaming creative does not match the ladder.** An off-ladder 2-rung HLS
  creative was stitched into every variant by nearest match, with no warning: the 360p rung received 540p at 2.08
  times its advertised `BANDWIDTH`. The reference encodes its demo creatives with each ladder kit.
- **FR-9: a "secret" flag or a redaction list for `HttpRequest.Headers`,** so that `MAKING_ADS_REQUEST` and
  `RAW_ADS_REQUEST` never log credentials in plain text (W-2 1.25).
- **FR-10: a Service Quotas `UsageMetric` and a published maximum** for `L-32FBC51C`, `L-B9D43015` and `L-C13EFDBD`,
  so that Service Quotas can alarm on them without metric math (W-2 3.8).

## Observed behaviour

Documentation gaps, and answers we measured to our own questions. None needs a workaround, but a deployment that
relies on the documentation can be surprised.

### AWS Elemental MediaTailor

- **`W-2 2.1` Quotas.** Service Quotas and `quotas.html` disagree on three limits. ADS redirects: `L-7202B1BD` says
  5, the user guide 7. ADS timeout: `L-B800E13A` says 3 s, the user guide 250 to 6,000 ms, and our configurations run
  at 2,000 ms. Source locations: `L-2290981E` says 100, the user guide 50.
- **`W-2 2.2` Headers on GET.** botocore says that `Headers`, `Body` and `CompressRequest` are "only eligible for POST
  requests"; the user guide says that headers go on GET and POST. The reference uses POST only.
- **`W-2 2.3` `CompressRequest`.** `ads-request-configuration` calls the member by its shape name,
  `CompressionMethod`.
- **`W-2 2.4` Yield Optimization.** The page gives a 15 s minimum threshold, but the API minimum of
  `MinimumUnfilledDurationSeconds` is 6. It calls `mimes` mandatory (it defaults to mp4), `devicetype` hardcoded (it
  can be overridden) and `bidfloor` required (it is optional, default 1.0).
- **`W-2 2.5` Personalization threshold.** `slate-management.html` says that the threshold applies only when slate
  is configured. The service abandoned breaks with `SlateAdUrl` unset (13 breaks per session, 2026-10-27). Two rows
  of its matrix invert the comparison; the botocore text is right.
- **`W-2 2.7` Alternate-media billing.** Alternate-media (audience) hours bill as extra STANDARD hours (+168.7 s in
  Cost Explorer). Only a blog says so.
- **`W-2 2.8` ADS log schema.** Its `oneOf` covers only the PRE_ADS hook events, `functionType` lacks
  `VAST_REQUEST` and `AWS_SERVICE_REQUEST`, and `adBreakCorrelationId` is absent (W-2 1.11).
- **`W-2 2.9` PAL nonce.** `gam-integration-pal.html` requires "a unique nonce" for each request, but PAL allows one
  nonce for several ad requests of one stream.
- **`W-2 2.10` Functions pages.** The troubleshooting page says that PRE_SESSION may write only `player_params`; the
  hook page also allows `manifest.*` and an `aws.*` allow-list. `Output` is described as both required and optional.
  The Elemental Inference integration page misspells `FunctionMappings`.
- **`W-2 2.12` `aws.` parameters.** `variables-service.html` says that they are "not forwarded to the origin
  server", but `aws.mediatailor.channel.timeDelay` on the session URL reaches every Channel Assembly request, as it
  must.
- **`W-2 2.13` `AFTER_LIVE_EDGE`.** The service suppresses breaks that start within the first part of the session
  that the value sets, as `ad-rules` describes, not the breaks "that start after the specified time window from the
  live edge".
- **`W-2 2.14` PRE_MANIFEST input.** Earlier and suppressed breaks come back with `mutable:false`. A break that the
  viewer joined mid-way is `mutable:true` only while its start is still in the manifest window; otherwise the hook
  does not run for it.
- **`W-2 2.15` Prefetch variables.** `variables-session.html` says that `session.uuid` and `session.user_agent` are
  not available for prefetch. MediaTailor substituted both on every Channel Assembly prefetch retrieval.
- **`W-2 2.17` Progressive fallback.** A ready progressive-only ad in VAST position 1 is used even when the
  streaming ads alone fill the avail, and a streaming ad is then trimmed (`AVAIL_DURATION_EXCEEDED`).
- **`W-2 2.18` The Functions HTTP client.** Every `HTTP_REQUEST` and `VAST_REQUEST` uses HTTP/1.1 over TLS 1.3, even
  when h2 or h3 is offered. The User-Agent is `Midas`, or the viewer's on `VAST_REQUEST` wrapper hops.
- **`W-2 2.19` Function cache partitions.** Each cold cache key caused 4 upstream calls at 100 and 300 sessions
  (2026-10-27), and 5 on three cold breaks (2026-10-29): concurrent misses from separate partitions, from different
  addresses within 5 to 30 ms. Size an upstream for about 4 or 5 calls per key, per configuration, per cold window
  ([release-controller.md](release-controller.md)).
- **`W-2 2.21` Query-parameter limit.** The 2,000-character limit applies to `manifest.*` parameters only. The
  service accepted and delivered 2,563 characters of `ads.*`, and the "double characters" rule does not apply to
  `--` or `__` in `ads.*` values.
- **`W-2 2.24` The `Requests` metric.** Its 1-minute Sum counts every request, `/v1/segment` redirects and
  multivariant requests included: 51,541 = 43,615 playlists + 7,922 redirects in one minute.
- **`W-2 2.25` `Origin.Latency`.** MediaTailor emits it, but no page documents it. Its SampleCount equalled Channel
  Assembly's `RequestCount` exactly over 120 minutes (36,307), which makes it a precise measure of origin load per
  configuration.
- **`W-2 2.26` `AdDecisionServer.Errors` and `.Latency`.** A valid VAST with no ads is not an "empty response": 2
  `EMPTY_VAST_RESPONSE` events counted 0. `AdDecisionServer.Latency`'s SampleCount matched `MAKING_ADS_REQUEST`
  minute by minute, failed requests and prefetch retrievals included.
- **`W-2 2.27` `AdDecisionServer.Ads`.** One sample is one filled break, at the break, prefetched or not: retrievals
  have no sample. The Sum of `Avail.FilledDuration` matched the sum of `FILLED_AVAIL.avail.filledDuration` to the
  millisecond (1,561,560 ms = 1,561.56 s).
- **`W-2 2.29` Mid-break joins.** With `AFTER_LIVE_EDGE` `00:00:00` and `PARTIAL_AVAIL`, MediaTailor asks for the
  break less the part that has aired, at segment granularity: 90,090 ms at +30 s into a 120.12 s break, and 48,054
  or 42,054 ms at +76 s. The aired part plays as slate, and the ads start at their first segment.
- **`W-2 2.30` Beacons of a mid-ad join.** With suppression `OFF`, a viewer who entered an ad mid-way got its
  midpoint, third-quartile and complete beacons, but no impression or start. Ads stitched before the viewer's first
  segment fired nothing.
- **`W-2 2.31` Prefetch times.** MediaTailor stores prefetch times in whole seconds, consistent with truncation:
  `…T13:31:33.900Z` reads back as `…:33Z`. The API reference types them as `Timestamp` with no precision. Our
  prefetcher compares starts in whole seconds.
- **`W-2 2.33` `allowMultipleAds`.** MediaTailor ignores `allowMultipleAds="false"` on a VAST Wrapper: a Wrapper that
  resolves to a 4-ad pod filled like the pod itself, with either value.
- **`requestId` spans decisions.** The ADS log gave one `requestId` to a `FILLED_AVAIL` and, 80 ms later, to the next
  break's prefetch retrieval (2026-11-06). It identifies the playlist request that set them off, not one ad
  decision; the documentation does not say so. Our joins take a break from the prefetch schedule, then the URL.
- **PRE_SESSION on a failed start.** MediaTailor creates the session and runs PRE_SESSION before it reads the origin,
  so an init whose origin answers 404 still runs the hook and commits its outputs (the player gets a 504
  `MANIFEST_ERROR`). The viewer client mints a new ticket before it retries ([identity.md](identity.md)).
- **Prefetch traffic shaping.** A SINGLE schedule with retrieval from 150 s to 30 s before the break and a 90 s
  `RETRIEVAL_WINDOW` spread 40 sessions' requests from 150 s to about 67 s ahead (p05 to p95: 59 to 72 s). Without
  shaping, all 40 came within 4 s of the window's start. The user guide does not say where in the window the spread
  starts.
- **`W-2 3.1` Device headers.** MediaTailor sends no `X-Device-IP` (130 of 130 `BEACON_FIRED` and 62 of 62 requests
  listed none) and, on the wire, no `X-Device-User-Agent` (W-2 1.24). VAST 4.x §1.1.3 says that stitchers "must
  include" both.
- **`W-2 3.2` Egress addresses.** `ip-ranges.json` has no MediaTailor service. Ad calls came from 18 addresses in 46
  calls, and from 24 addresses for 21 sessions, all in untagged EC2 prefixes. The viewer-IP rule finds MediaTailor's
  hop by position for that reason ([pds.md](pds.md#the-viewer-ip-rule)).
- **`W-2 3.3` Measurement.** AWS states no plans for MRC accreditation or ads.cert. Without them and 3.1, MediaTailor
  beacons cannot be a billing count; the reference bills by the counterparty's count ([ADR-0022](adr/0022-delivery-states-billing-and-the-hash-chained-ledger.md)).
- **`W-2 3.4` Google Ad Manager certificate.** `gam-integration-ssl.html` does not say which beacons, Function calls
  and URL rewrites the certificate covers.
- **`W-2 3.5` Beacon macros.** The documentation does not say which macros MediaTailor fills in server-side beacon
  URLs, such as `[CACHEBUSTING]`, `[TIMESTAMP]`, `[DEVICEIP]`, `[DEVICEUA]` and `[IFA]`.
- **`W-2 3.6` Beacon retries.** MediaTailor retries beacons for up to an hour, and an operator cannot limit that per
  configuration. Some ad servers ask for events "at the moment" they are registered.
- **`W-2 3.7` Insertion price.** It is unclear whether Channel Assembly sessions, which are classified LIVE, cost
  $0.50 or $0.25 per 1,000 insertions.

### Channel Assembly

- **`W-2 2.6` Scheduling rules.** `channel-assembly-adding-programs.html` requires a start "15 minutes or later", but
  the service accepts more than 1 minute for VOD programs on a running channel. CloudFormation's
  `ScheduleConfiguration` says that the channel must be stopped, but `CreateProgram` works on running channels.
- **`W-2 2.11` Time shift.** The time-shift pages describe a `start` parameter and a 24-hour window that the service
  does not support: it ignores `start=`. The control that works is `aws.mediatailor.channel.timeDelay=<s>`, which
  returns 404 beyond the channel's history. `MaxTimeDelaySeconds` is capped at 21,600, on STANDARD channels only.
- **`W-2 2.23` Origin load.** `ca-setting-up-emt-ssai.topic.html` says that ad insertion "can generate additional
  origin requests", and advises a CDN between Channel Assembly and ad insertion. SSAI never made more origin requests
  than players did: 0.95 origin requests per player request at 5 sessions, 0.105 at 400 and 0.030 at 1,600. The
  reuse lasts up to the origin's `max-age`.
- **`W-2 3.9` Prefetch and Yield Optimization.** Prefetch worked on a Channel Assembly channel: RECURRING and SINGLE
  schedules (SINGLE matched on `scte.event_id`) filled every prefetched break, and first-sight skips fell to 0. The
  user guide names only "linear channels" for prefetch, and says that Yield Optimization is not supported with
  prefetch.
- **Programs after they air.** Channel Assembly deletes LINEAR programs about 13 to 15 minutes after they finish
  airing, even on a stopped channel, which is why the media library is the schedule of record
  ([channel-assembly.md](channel-assembly.md#the-schedule-of-record-and-the-epg)). On a BASIC channel, programs were
  still present 44 minutes after they aired; we have not found the cause.

### Amazon Bedrock and Bedrock AgentCore

- **`W-7 Q4` The principal of a limit.** `$.context.iam.principal` resolves to the IAM role's ARN, not the STS
  session's, so every session of a role shares one bucket. That suits limits per task, but it is not documented as a
  contract.
- **`W-7 Q7` Routing of an unqualified id.** The connector page says "at random" and the provider page "round-robin".
  The 200 a second quota is described as "tool-call/tool-list requests per gateway", and it is unclear whether it
  covers inference. Not tested.
- **`W-7 Q9` Thinking and `maxTokens`.** With Claude Haiku 5.5's adaptive thinking, `effort` low, a JSON output
  schema and `maxTokens` 4,000, 59 of 206 Converse requests (29 %) returned `stopReason=max_tokens` with an empty
  text block; with thinking disabled, all 206 were valid. A change to the thinking setting, or a per-request output
  schema, rewrote the prompt cache. The recommender runs with thinking on and `max_tokens` 8,000, sized by trial
  ([ADR-0029](adr/0029-per-task-model-registry.md)).
