# Workarounds to remove

FAST Channels works around the AWS product behaviour it depends on, where that behaviour is a defect or is not
documented, so the system works today. Each workaround is marked where it lives, so it can be deleted when AWS fixes
the product:

```text
OPTIMIZATION(aws): <id> <what it works around>
OPTIMIZATION(oss): <id> <what it works around>      (an open-source dependency)
```

The id names the report: `W-2 <n>` is an item of our support case for AWS Elemental MediaTailor and the services
around it, `W-11` the feedback for the
[Guidance for Deploying a Prebid Server on AWS](https://github.com/aws-solutions-library-samples/prebid-server-deployment-on-aws),
`W-12` the Prebid Server Java issue, and `FINDINGS <module>` a build note. List every marker with:

```bash
git grep -n "OPTIMIZATION("
```

`tests/e2e/test_workarounds.py` checks that every id in the code has a row here and every row has a marker in the
code. Our own misconfiguration is fixed, not listed here.

## AWS Elemental MediaTailor and Channel Assembly

| Id | AWS behaviour | Our workaround | Where | When AWS fixes it |
|---|---|---|---|---|
| `W-2 1.30` | `ConfigureLogsForChannel` and `ConfigureLogsForPlaybackConfiguration` call `iam:CreateServiceLinkedRole` with the caller's credentials, even when the role exists, and fail without it. | The roles that configure logs may create MediaTailor's service-linked role (its exact ARN, condition `iam:AWSServiceName`). | `infra/.../operator/tools.py` (the agent's channels tool), `infra/.../release/controller.py` (the apply and promote roles) | Delete the three grants and `MEDIATAILOR_SLR`. Keep `test_08_agent_channel`. |
| `W-2 2.28` | An `HTTP_REQUEST` step that times out inside a `SEQUENTIAL_EXECUTOR` counts in no error metric. | The smoke judge and the pointer poll read each PRE_ADS step's `Function.Latency` against its own timeout: two minutes at the timeout fail the smoke or stop the deployment. | `core/.../monetization/smoke.py` (`TIMEOUT_PERIODS`, `request_timeouts`, `timed_out`), `functions/.../release/promote.py` (`step_maxima`, `timed_out_steps`), the `GetMetricStatistics` grant in `infra/.../release/controller.py` | Alarm on the error metric instead; delete the latency checks and the grant. |
| `W-2 1.22`, `W-2 1.23` | Body-template values are not JSON-escaped, and a body that does not render to JSON skips the ad request silently. | The POST body holds only MediaTailor's own variables, each inside a JSON string; player values travel in headers; no PRE_ADS function rewrites the body or its variables (R-P1). | `core/.../monetization/playback.py` (`BODY_VARIABLES`, `ads_body`), `core/.../monetization/validators.py` (`check_body_template`) | Relax R-P1: escaped player values and unquoted numbers in the body. The module still expects `pod_s` as a string. |
| `W-2 1.23` | A skipped ad request (an invalid body, or a PRE_ADS hook that spent the budget, 1.7) logs a marker and no error, and no metric counts it. | The silent-loss detector counts markers without `MAKING_ADS_REQUEST` per sibling and alarms; the release smoke fails a break that made no ad request. | `infra/.../release/detectors.py` (`MarkerDetector`) | Alarm on the AWS metric or log event for a request that was not sent; delete `MarkerDetector`. |
| `W-2 1.6` | `Live/VodMaximumAdsPersonalizationTimeMilliseconds` is a wall clock that includes hook time. | R-P3: the maximum covers the PRE_ADS executor's timeout plus the ADS timeout. | `core/.../monetization/validators.py` (`check_live_maximum`) | If the budget counts only ADS time, R-P3 becomes maximum ≥ ADS timeout; resize the maxima. |
| `W-2 1.26` | `PutFunction` enforces neither the documented 1,000-character limits nor the JSONata allow-list; the failure comes at run time and fails open. | R-F1 and R-F2 check the limits and the allow-list before any `PutFunction`. | `core/.../monetization/validators.py` (the limits), `core/.../monetization/jsonata_allowlist.py` | Optional: keep as a pre-flight, or drop once `PutFunction` rejects them. |
| `W-2 1.9` | PRE_MANIFEST drops the first injection of a creative URL and transcodes later ones despite `NONE`. | R-P4 forbids PRE_MANIFEST functions. | `core/.../monetization/validators.py` (`check_ad_behaviour`) | Drop the R-P4 clause. |
| `W-2 1.1` | `NEW_CREATIVE` skips continue after a creative is ready (FR-2: concurrent first sights each start a transcode). | Prefetch (SINGLE schedules) also prepares the creatives before the break airs. | `infra/.../config.py` (`ReleasePrefetch`) | Creative readiness stops being a reason for prefetch; it stays for shaping the break herd (ADR-0033). |
| `W-2 1.12` | A SINGLE prefetch rounds `session.avail_duration_ms` to whole seconds. | The schedule sends `session.avail_duration_secs` too, which the body uses. | `core/.../monetization/prefetch.py` (`schedule_request`) | Keep; the value stays correct either way. |
| `W-2 2.16` | `ListPrefetchSchedules` without `ScheduleType` hides RECURRING schedules (the default is not documented). | Every listing passes `ScheduleType=ALL`. | `functions/.../release/prefetch.py`, `functions/.../release/reaper.py` | Keep, or drop the argument once the documented default is `ALL`. |
| `W-2 1.32` | The vended ADS log can leave out a whole break that MediaTailor requested and filled. | An hourly reconciliation compares `Avail.FilledDuration` with the log's `FILLED_AVAIL` and alarms (`<prefix>-release-log-loss`); the smoke passes an unlogged break whose request the PDS recorded. | `functions/.../release/reconcile.py`, `infra/.../release/detectors.py` (`LogReconciler`) | Delete `LogReconciler`, `reconcile.py` and the alarm; the smoke requires the request in the log. |
| `W-2 1.11` | `AD_MARKER_FOUND` is logged twice per Channel Assembly break, and a prefetched break's events carry `prefetchScheduleName` instead of `adBreakCorrelationId`. | The smoke judge and the detector key breaks on `coalesce(adBreakCorrelationId, prefetchScheduleName)` and count only the first marker (no `,DURATION=`). | `functions/.../release/smoke.py` (`QUERY`), `infra/.../release/detectors.py` (`BREAK_START`) | Key on `adBreakCorrelationId` alone and drop the `DURATION` filter. |
| `W-2 1.3` | A channel-policy change on a running channel breaks sessions of configurations it keeps, and takes minutes to apply (1.4, 1.5). | One static sibling pattern `<channel>-p-?`, set when the channel is created; releases never call `PutChannelPolicy`; the channel guard refuses a pattern change while the channel runs. | `core/.../specs.py` (`SIBLING_PATTERN`), `functions/.../custom_resources/channel_lifecycle.py` (`GUARDED`), `infra/.../release/guards.py` | Let a running channel take a pattern change; releases could then list exact siblings. |
| `W-2 1.25` | The default ADS log stores request-header values, the origin secret and the playback ticket among them, in plain text. | Log deliveries select fields without `requestHeaders` and `rawAdsRequest`; the events that log the ticket are never on for a ticketed sibling. | `core/.../mediatailor_logs.py` (`EXCLUDED_ADS_LOG_FIELDS`), `core/.../monetization/playback.py` (`TICKET_LOG_EVENTS`) | With header redaction the secret reason goes; keep the exclusion while viewer data must stay out of logs. |
| `W-2 1.20` | `PutFunction` rejects hyphenated `FunctionId` values, and the pattern is not published. | Function ids use the prefix with `-` replaced by `_`. | `core/.../monetization/functions.py` (`FUNCTION_ID_PATTERN`) | Keep; publish-only fixes change nothing. |
| `W-2 1.16` | `PutFunction` with a non-empty `Tags` map fails on an existing id. | An existing Function is compared without its tags. | `functions/.../release/apply.py` (`put_function`) | Compare with tags. |
| `W-2 2.32` | `ListPrefetchSchedules` authorizes on `prefetchSchedule/*`, not on the playback configuration the Service Authorization Reference names. | An account-wide list grant. | `infra/.../release/prefetch.py`, `infra/.../release/controller.py` (the reaper) | Narrow to the siblings. |
| `W-2 2.22` | `session.client_ip` is the leftmost `X-Forwarded-For` entry, which the client controls. | The viewer address comes from the chain CloudFront appends, read at Prebid Server. | `core/.../pds/xff.py` (`viewer_ip`) | If MediaTailor uses a trusted entry, `session.client_ip` in a header could replace the chain rule. |
| `W-2 2.20` | A zero or missing `CUE-OUT` duration arrives as 300 s (undocumented). | 0 s and 300 s are never grid durations: the module answers them with an empty pod and the slate plays. | `core/.../monetization/validators.py` (`MISSING_DURATION_SECONDS`) | Keep; update the constant if the default changes. |
| `W-2 3.8` | Service Quotas has no usage metric for MediaTailor's request quotas (FR-10). | A Metrics Insights sum of every configuration's `Requests` against the default quota. | `infra/.../ops/detectors.py` (`AccountQuotaAlarms`) | Alarm on the usage metric; delete the constants and the query. |
| `W-2 FR-1` | MediaTailor endpoints are public, and a failed PRE_SESSION keeps player-set `aws.*` (ad-free sessions, 1.10 DEBUG logs). | An hourly zero-ad-sessions detector and lake view. | `infra/.../ops/detectors.py` (`ZeroAdsDetector`), `functions/.../ops/zero_ads_sessions.py` | Retire the detector and the view with authenticated session initialization. |

## Other AWS services

| Id | AWS behaviour | Our workaround | Where | When AWS fixes it |
|---|---|---|---|---|
| `W-2 4.7` | A 0-minute deployment ignores the monitors, `AppConfig.AllAtOnce` bakes 10 minutes under them (no other deployment may start), and a monitor in `INSUFFICIENT_DATA` rolls a deployment back. | Our own LINEAR 100 %, 0-minute, no-bake strategy for rollbacks, first pointers and flags; R-A1 never deploys a promotion all at once; R-A2 wants `notBreaching` monitors. | `infra/.../release/pointer.py`, `functions/.../release/promote.py` (`_at_once`), `infra/.../ops/flags.py` (`_at_once`), `core/.../monetization/validators.py` (`check_monitors`) | Re-check against the corrected pages; drop the custom strategies only if AllAtOnce stops baking under monitors. |
| `W-2 4.13` | AppConfig reports a denied `DescribeAlarms` call as a composite alarm that does not exist, and rolls back. | The monitor role may call `cloudwatch:DescribeAlarms` on `*`. | `infra/.../ops/flags.py` | Scope it to the alarms. |
| `W-2 4.14` | AppConfig authorizes some actions on the application's and the environment's ARNs too (undocumented). | The promote grants name those ARNs. | `infra/.../release/controller.py` (`_grant_pointer`) | Narrow to the documented resources. |
| `W-2 4.12` | CloudWatch Logs decrypts a log group encrypted with a customer managed key for any IAM reader, without `kms:Decrypt`. | An explicit IAM deny of the log reads on the siblings' log group for every role but its readers. | `infra/.../release/guards.py` (`LOG_READ_ACTIONS`) | If the key gates readers, the deny can go. |
| `W-2 4.11` | AWS Budgets actions reject daily budgets. | A monthly budget at 31 times the daily cap; the token counter enforces the day. | `infra/.../models/caps.py` (`BUDGET_DAYS`) | A daily budget with the action; delete `BUDGET_DAYS`. |

## The Prebid Server guidance and open-source dependencies

| Id | Behaviour | Our workaround | Where | When it is fixed |
|---|---|---|---|---|
| `W-11` | The guidance hard-codes its stack names, its Glue database and the CDK bootstrap; has no WAF extension point; ships simulator creatives that cannot fill a pod; builds one release on one JDK; deploys at 50 % minimum healthy; keeps the previous image on redeploy; logs every request header in its analytics; needs its own VPC for the simulator; and does not warm a new task. | Overlays O1 to O7, `--no-previous-parameters`, `EnableLogAnalytics=false`, the Lambda simulator and the warm-up replay. | `vendor/prebid-guidance/PIN.toml` (the overlays), `scripts/_demand.py`, `infra/.../demand/simulator.py` (`LambdaSimulator`), `infra/.../demand/warmup.py` (`WarmUp`) | Drop each overlay or setting as the guidance ships the fix. |
| `W-12` | Prebid Server Java creates an adapter's per-request-type metric key in a map without a lock: the first concurrent auctions that reach a new key can fail with HTTP 500 (`ConcurrentModificationException` in `AdapterTypeMetrics.requestType`). | The warm-up also sends the warm stored request, first one at a time: its slot answers every configured adapter from a stored bid response, so a new task creates the keys without calling a partner. The module keeps only stored-response bidders on a warm request; `pbs_config` refuses any adapter the zero-recorder gate has not covered. | `core/.../pbs_config.py` (`warm_stored_request`), `pbs/fast-ctv/.../core/WarmUp.java`, `infra/.../demand/warmup.py` | Delete the warm stored request, its stored responses, `WarmUp` and the sequential warm requests; keep the JIT replay. |
| `FINDINGS M13-02` | Strands Agents derives prompt caching and the tool-result status from the model id, so an inference-profile ARN silently turns them off. | The agent sets both from the profile's model. | `agent/.../model.py` (`bedrock_model`) | Pass the profile ARN alone. |

## Agentic selling: the advertising agents guidance and the Prebid Sales Agent

The selling module's workarounds ([selling.md](selling.md#workarounds-to-remove)). `W-10` is the feedback for the
[Guidance for Advertising Agents on AWS](https://github.com/aws-solutions-library-samples/guidance-for-advertising-agents-on-aws),
`W-8` the one for the [Prebid Sales Agent](https://github.com/prebid/salesagent).

| Id | Behaviour | Our workaround | Where | When it is fixed |
|---|---|---|---|---|
| `W-10` | The reference buyer refuses `create_media_buy` without an approved `check_governance`, even with no governance agent (item 1); its A2A runtime fails every turn without a sessions table (item 2); `deploy_all.py` escapes a permissions boundary and the agents are not published as images (items 3, 4); and it has no creative tasks (item 6). | The governance agent is deployed with the buyer; the module creates the sessions table; the stage builds the pinned upstream code and deploys the agents with its own CDK; booked buys serve the ladder kit's demo creatives and stay `pending_creatives`. | `infra/.../selling/agents.py`, `infra/.../selling/image_build.py`, `vendor/agentic-selling`, `core/.../selling/booking.py` | Make governance optional, drop the table, deploy the published images, serve the buyer's creatives. |
| `W-8` | The seller blocks booking in single-tenant mode (A7), rebuilds `@target-uri` from the `X-Forwarded-Proto` the load balancer sets to `http` (A8), dials the public reference creative agent in testing mode (A9), publishes the tenant's host as a tag-authorized product's publisher domain (A10), and has no ad-server adapter for MediaTailor and the PDS (A11). | `ADCP_MULTI_TENANT`; `Fly-Forwarded-Proto: https` from CloudFront; a `CREATIVE_AGENT_URL` that cannot resolve; products that name their properties; the bridge, which reads buys from the seller's database and writes delivery to the mock adapter's seeded-delivery rows. | `infra/.../selling/seller.py`, `vendor/agentic-selling/seller/fast/bridge.py` | Drop each setting as the seller ships the fix; a `mediatailor_pds` adapter replaces the bridge. |

In the paths, `core/...` is `core/src/fast_channels/core`, and so on for `functions`, `infra`, `agent` and `pds`.
