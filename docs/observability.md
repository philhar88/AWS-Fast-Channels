# Observability and traceability

Every service in FAST Channels logs to CloudWatch Logs (CloudFront to S3) with a retention, traces where the service
supports tracing, and one correlation id threads an asset, a schedule or a viewing session through all of them. This
page lists what logs where, the correlation contract, the saved queries and `scripts/trace.py`, and the dashboard,
alarms and notifications.

- [Logging and tracing matrix](#logging-and-tracing-matrix)
- [Correlation contract](#correlation-contract)
- [Tracing an id](#tracing-an-id)
- [Dashboard and alarms](#dashboard-and-alarms)
- [Lake](#lake)
- [Metrics](#metrics)
- [What the agent runtime records](#what-the-agent-runtime-records)
- [Audit trail](#audit-trail)
- [Transaction Search](#transaction-search)

## Logging and tracing matrix

Retention is `observability.log_retention_days` (default 7) for every log group and for the logs bucket's objects.
No log group appends to the account-wide CloudWatch Logs resource policy: every vended-log destination carries its own
log-group-scoped policy.

| Service | Logs | Where | Tracing and metrics |
|---|---|---|---|
| Lambda functions | Powertools JSON (Lambda JSON log format, INFO): `correlation_id`, `service` (e.g. `ingest.prepare`), `xray_trace_id`, plus `asset_id`, `ladder_version`, `tool` or `channel` where relevant | One explicit log group per function (CloudFormation-named, e.g. `FastChannels-Media-…`) | Active tracing (service segments, no X-Ray SDK; [ADR-0004](adr/0004-tracing-without-the-xray-sdk.md)); custom metrics in `FastChannels` |
| Step Functions | `ALL` events with execution data | `/aws/vendedlogs/states/<prefix>-ingest`, `-ladder-provisioner`, `-ladder-kit-waiter`, `-reaper`, `-channel-census` | X-Ray tracing |
| MediaConvert | Job state changes of this deployment's jobs (filtered on `userMetadata.app`), including warnings | `/aws/vendedlogs/events/<prefix>-mediaconvert` | `AWS/MediaConvert` per queue |
| EventBridge bus | `INFO` logs with event details; an archive of every event | `/aws/vendedlogs/events/<prefix>-fast-channels` | |
| Channel Assembly | `AS_RUN` events (`VodProgramStart`, ad breaks, filler) | `MediaTailor/Channel/AsRunLog` (shared by the account and region) | `AWS/MediaTailor` channel metrics (requests, 4xx, 5xx) |
| MediaTailor ad insertion | Vended logs: ad decision server, manifest service and transcode logs, for `observability.mediatailor_log_percent` of sessions | `/aws/vendedlogs/mediatailor/<prefix>` | `AWS/MediaTailor` per playback configuration (ADS errors, timeouts, fill rate); no X-Ray, joined on the session id |
| CloudFront | Standard logs v2, Parquet with Hive partitions, CMCD fields | Logs bucket `AWSLogs/aws-account-id=<acct>/CloudFront/…` (query with Amazon Athena) | CloudFront metrics (us-east-1) |
| S3 | Server access logs of the ingest and media buckets | Logs bucket `s3/ingest/`, `s3/media/` | |
| AgentCore Runtime | Application log records through ADOT (`otel-rt-logs` stream, with `correlation_id`, `actor`, `mode`, `model_id`); message content only with `observability.capture_prompts` ([below](#what-the-agent-runtime-records)); vended application and usage logs | `/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT`; `/aws/vendedlogs/bedrock-agentcore/<prefix>/runtime/<LOG_TYPE>` | OpenTelemetry spans with Transaction Search only |
| AgentCore Gateway, Memory | Vended application logs; the Gateway's logs record every request and response body, with no caller identity | `/aws/vendedlogs/bedrock-agentcore/<prefix>/<gateway\|memory>/APPLICATION_LOGS` | Trace deliveries with Transaction Search only |
| Cognito | Administrative API calls in CloudTrail; the Essentials feature plan does not export user-activity logs | CloudTrail | |

## Correlation contract

| Thread | Identifier | Set by | Appears in |
|---|---|---|---|
| Ingest | `correlation_id` = Step Functions execution name | The sidecar or `IngestRequest`, else derived from the S3 event | Every pipeline Lambda log line, the state machine logs, MediaConvert `UserMetadata.correlation_id` (job events), the library asset, the VOD source's `fast:correlation-id` tag, `Asset Ready`/`Asset Failed` |
| Agent conversation | AgentCore Runtime session id | The client (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`) | Runtime logs; the `correlation_id` of every tool call and its Lambda logs; ingest and kit executions started by tools; `plan_id` (`<channel>.<8 hex>`) |
| Tool call | Gateway request id | AgentCore Gateway | `gateway_request_id` (with `mcp_message_id`, `gateway_id`, `gateway_target_id`) on every log line of the call; `request_id` in the Gateway's `APPLICATION_LOGS` |
| Program | Program name `<yyyymmddThhmmss>-<asset_id>` | `schedule___plan` | `PROGRAM#` item, Channel Assembly `CreateProgram`, the as-run log (`programName`), the EPG feeds |
| Ad break | SCTE-35 `SegmentationEventId` and UPID (`FST3` + content id) | `schedule___plan` (deterministic) | HLS `DATERANGE` `ID`/`SCTE35-OUT`, the ADS request (`evid=`, `upid=`), MediaTailor `MAKING_ADS_REQUEST`, the CloudFront log of the VAST request |
| Viewing session | MediaTailor session id = `[session.uuid]` = `aws.sessionId` | MediaTailor | The ADS request (`sid=`), vended logs (`sessionId`), CloudFront logs (query string; `cmcd-session-id` when the player sends CMCD with that id) |
| CDN request | `x-edge-request-id` = `Server-Timing: cdn-rid` = `x-amz-cf-id` | CloudFront | Access logs; response headers the player sees |
| Lambda invocation | X-Ray trace id | Lambda | `xray_trace_id` on every Powertools log line; the X-Ray trace map |

From a viewer complaint to the schedule: the player's `cdn-rid` or session id finds the CloudFront and MediaTailor
records; their `evid` and `upid` name the break; the break's event id belongs to one `PROGRAM#` item, which names the
asset; the asset's `correlation_id` finds its encode.

## Tracing an id

### `scripts/trace.py`

```bash
make trace ID=big-buck-bunny-1a2b3c                               # asset id
uv run python scripts/trace.py seed-sintel-1a2b3c4d --since 6h    # ingest correlation id (execution name)
uv run python scripts/trace.py 49f0c250-9e3c-4997-a723-3cd95eff9d42   # MediaTailor playback session id
uv run python scripts/trace.py 20261016T120000-sintel-1a2b3c --full   # program name: whole log messages
uv run python scripts/trace.py <runtime session id> --prefix news     # another deployment
```

The script finds the deployment's log groups (tagged `fast:app=<prefix>`, or named by the stacks and services as in
the matrix above), expands the identifier with what the media library links to it (an asset's ingest correlation
ids; `evid=<event id>` for each break of a program), runs one Logs Insights query per 50 log groups over the last
`--since` (default 24 h) and prints one time-ordered timeline (abridged):

```
2026-10-16T12:00:01.112Z  ingest.trigger                      INFO | starting ingest | …
2026-10-16T12:00:02.430Z  states/fast-ingest                  ExecutionStarted | …
2026-10-16T12:00:05.901Z  events/fast-mediaconvert            MediaConvert Job State Change | PROGRESSING
…
```

Each line shows the UTC time, the source (the Powertools service of a function, else the log group) and a summary of
the record (`--full` prints the whole message). It exits with status 1 when nothing matched. CloudFront logs are
Parquet objects in S3 and are not searched; query them with Amazon Athena (Hive-partitioned Parquet), for example by
`cmcd-session-id` or the `sid=` of an ad request.

### Saved Logs Insights queries

The Media stack saves four queries under `FastChannels-<prefix>/` in the CloudWatch console (Logs Insights → Saved
queries). Each takes one parameter and covers the media plane's log groups: the bus, MediaConvert, the pipeline's
state machines and functions, the ladder provisioner, MediaTailor's vended logs and the as-run log. The Operator
stack's logs (tool functions, Runtime) are not in them, because the Media stack cannot reference its dependants;
use `scripts/trace.py` for agent sessions.

| Query | Parameter | Use |
|---|---|---|
| `Trace asset` | `asset_id` | An asset id or ingest correlation id through the pipeline |
| `Trace session` | `session_id` | A MediaTailor playback session (ADS requests, fills, beacons); the CDN's side is in the Parquet logs |
| `Trace program` | `program` | A program name or the SCTE-35 event id of one of its breaks |
| `Trace agent session` | `session_id` | The media-plane side of an AgentCore session: the ingests and kits it started |

Useful ad-hoc queries on `/aws/vendedlogs/mediatailor/<prefix>`:

```
filter eventType = "FILLED_AVAIL"
| stats count() as breaks, avg(avail.fillRate) as fill by avail.skippedAds.0.skippedReason

filter eventType = "MAKING_ADS_REQUEST" | fields @timestamp, sessionId, adsRequestUrl
```

## Dashboard and alarms

The dashboard `FastChannels-<prefix>-<region>` (output `DashboardUrl`) shows the pipeline, MediaConvert, every IaC
channel, ad insertion, running channels and the CDN. Alarms send both `ALARM` and `OK` to the SNS topic
`<prefix>-alerts`:

| Alarm | Fires when |
|---|---|
| `<prefix>-ingest-failed`, `<prefix>-ladder-provisioning-failed` | An execution of the state machine failed |
| `<prefix>-ingest-dead-letters` | The ingest dead-letter queue holds a message: an upload or `Ingest Requested` event that never started an execution (a failed trigger invocation or EventBridge delivery). It stays in `ALARM` until the messages are redriven or purged |
| `<prefix>-mediaconvert-job-errors` | A job on the deployment's queue errored |
| `<channel>-errors` | Channel Assembly 4xx + 5xx responses reach 10 in 5 minutes |
| `<channel>-manifest-requests` | Manifest requests stay at 80 % of the 50 TPS per-channel quota for 3 minutes |
| `<channel>-ads-errors` | ADS errors plus timeouts reach 5 in 5 minutes |
| `<channel>-fill-rate` | `Avail.FillRate` stays below 0.5 for 15 minutes |
| `<prefix>-channel-hours` | Running channel-hours in a day exceed 24 (a cost guard; raise it for production fleets) |
| `<prefix>-cdn-5xx` | CloudFront 5xx error rate (outside us-east-1: `<prefix>-<region>-cdn-5xx` in the `…-Edge-<region>` stack, with its own topic `<prefix>-<region>-alerts`) |
| `<prefix>-autopilot-errors`, `<prefix>-autopilot-dead-letters` | With an autopilot only: a run failed (it is retried once), or failed again and was dead-lettered, so the channel's schedule was not extended |

`Asset Failed`, `Ladder Failed` and failed `Autopilot Run` events are forwarded to the same topic as JSON, by rules
that publish through a role of their own (the topic admits no service principal). Set
`notifications.email` to subscribe an address, and confirm the subscription email (two of them outside us-east-1,
where the Edge stack has its own topic). Per-channel alarms exist for IaC channels only; agent channels appear in the
running-channel census and the channel-hours alarm.

Channel Assembly publishes no metric while a channel runs without viewers, so a `ChannelCensus` Express state machine
counts this deployment's `RUNNING` channels (by the `fast:app` tag) every 5 minutes into
`FastChannels/RunningChannels`, which the channel-hours alarm sums.

With the ops module (`modules.ops.enabled`) the census also announces when the count crosses zero: it compares the
count with the last one it recorded in the SSM parameter `/<prefix>/ops/running-channels`, and when one of them is 0
and the other is not it puts a `Census Changed` event (`running`, `previous`, `correlation_id` = the census execution
name; [contract](../contracts/events.schema.json)) on the deployment's bus, then records the new count. An event is
therefore at most one census interval (5 minutes) late, and a failed put is repeated by the next census, so consumers
(Prebid Server scaling, provisioned concurrency, canaries) must treat it as at-least-once. The parameter always holds
the latest count; read it instead of listing channels.

## Lake

With the ops module (`modules.ops.enabled`) each stage has a lake for Amazon Athena: the bucket `LakeBucketName`
(Data stack; versioned, SSE-S3, access-logged to `s3/lake/`, objects expiring after `modules.ops.lake_retention_days`),
the Glue database `<prefix>_lake` and the workgroup `<prefix>-lake` (Ops stack outputs `LakeDatabase`,
`LakeWorkGroup`). The workgroup enforces its settings, keeps results as Athena managed query results and cancels a
query that would scan more than 10 GiB. Tables use partition projection, so nothing crawls and no partition needs
adding; filter on the partition keys to keep scans small.

| Table or view | What | Partitions |
|---|---|---|
| `cloudfront_access` | The media distribution's standard logs v2 (the v3.0 Parquet in the logs bucket), every field except `c-ip`, `cs-uri-query` and `x-forwarded-for` | `year`, `month`, `day`, `hour` (UTC, zero-padded strings) |
| `mediatailor_ads` | Copies of every IaC playback configuration's ad decision server logs, JSON under `mediatailor/ads/`, without `requestHeaders` and `rawAdsRequest` | `datehour` (`yyyy/MM/dd/HH`, UTC) |
| `mediatailor_manifest` | The same for the manifest service logs, under `mediatailor/manifest/`, without `mediaTailorPath`, `input` and `http` | `datehour` |
| `v_breaks` | One row per aired break by SCTE-35 event id (`evid`): UPID, channel, the ad marker's `START-DATE`, the first ADS request, configurations, sessions, ADS requests, filled avails and seconds | — |
| `v_fill_by_pc` | Per playback configuration and hour: breaks, sessions, ADS requests, filled avails, avail and filled seconds, fill rate | — |

Column names are the log fields in lower case (`eventtype`, `adsrequesturl`, `cs_uri_stem`, `cmcd_session_id`).
Nested log objects (`avail`, `vastresponse`, `beaconinfo`) are JSON text: read them with `json_extract_scalar`. The
excluded fields never reach the lake: the ADS request headers hold the viewer's address and user agent (and, on
monetized configurations, header secrets and the playback ticket), the manifest fields hold the viewer's request path
and query and the session-initialization hook's HTTP exchange, and `cs-uri-query` records the viewer's original
query (DESIGN §5.7). The CloudWatch Logs copies of the same logs are v3.0's (`/aws/vendedlogs/mediatailor/<prefix>`).

```sql
SELECT evid, upid, started_at, sessions, ads_requests, filled_avails, filled_seconds
FROM v_breaks ORDER BY started_at DESC LIMIT 20;

SELECT configuration, hour, breaks, fill_rate FROM v_fill_by_pc ORDER BY hour DESC;

SELECT eventtype, count(*) FROM mediatailor_ads
WHERE datehour >= date_format(current_timestamp - INTERVAL '1' HOUR, '%Y/%m/%d/%H') GROUP BY eventtype;
```

## Metrics

Namespace `FastChannels`. A CloudWatch metric is identified by all its dimensions, so alarms and queries must name
every one: Powertools adds `service`, the function's handler module, to each metric it emits.

| Metric | Dimensions | Emitted by |
|---|---|---|
| `AssetReady`, `AssetFailed` | `service` = `ingest.publish`, `kind` (`content`, `slate`, `ad`) | Ingest pipeline |
| `LadderProvisioningStarted`, `LadderReady`, `LadderFailed` | `service` = `ingest.kit` | Ladder provisioner |
| `ToolSucceeded`, `ToolFailed` | `service` = `tools.<target>`, `tool` (`<target>___<tool>`) | Every agent tool call |
| `ProgramsAired` | `service` = `as_run_reconciler` | As-run reconciler |
| `AutopilotSucceeded`, `AutopilotFailed`, `AutopilotPublishes` | `service` = `autopilot`, `channel` | Autopilot runs |
| `ReapedChannels`, `ReapedPlaybackConfigurations`, `ReapedVodSources`, `ReapedSourceLocations`, `ReapedJobTemplates` | `service` = `custom_resources.reaper` | Runtime-resource reaper (stack deletion) |
| `RunningChannels` | `App` = `<prefix>` | Channel census |

Only `RunningChannels` names the deployment. The other metrics carry no prefix, so deployments that share an account
and region add up in them; tell them apart in the functions' logs (each deployment has its own log groups).

## What the agent runtime records

ADOT, under which the Runtime runs, would record message content by default: the system prompt, every operator
message, tool result and model reply in the `otel-rt-logs` stream, and the same content on the agent's spans. Anyone
with `logs:GetLogEvents` on the log group would then read every operator's conversation, although AgentCore Memory
keeps conversations apart per actor. So the agent switches content capture off at startup unless the Runtime's
`CAPTURE_PROMPTS` is `true`, which the deployment sets from `observability.capture_prompts`:

| `capture_prompts` | Message content in Runtime logs and spans |
|---|---|
| unset (default) | Only when `observability.transaction_search` is `enable` or `existing`: the online evaluations score that content |
| `false` | Never; online evaluations then have no content to score |
| `true` | Always, for debugging. Turn it off again afterwards: the log group keeps what was captured for its whole retention |

Without content, the Bedrock log events keep tool call ids and tool names, and the spans keep their structure, tool
names, token counts, `session.id` (the correlation id) and `actor.id`, with content shown as `[REDACTED]`. The
agent's own records, such as `run started` (`correlation_id`, `actor`, `mode`, `model_id`), never carry content.

## Audit trail

The Gateway validates the caller's token and authorises the call with Cedar, but passes no caller identity to the tool
functions, and its own logs name no principal. What records a change:

- **`tool audit`**: every call of a write or destructive tool logs one record in its function's log group, with the
  tool, its access and approval class, the arguments and the outcome (`ok` or the error code), next to the call's
  `correlation_id`, `gateway_request_id` and `xray_trace_id`.
- **Gateway `APPLICATION_LOGS`**: the request and response of every call (`request_id` = `gateway_request_id`).
- **Runtime `run started`**: for agent runs, the actor (the token's `sub`, or the autopilot's `client_id`) of the
  session whose id is the tool calls' `correlation_id`.

The console's own Start and Stop buttons call the Gateway directly, with `console-<uuid>` correlation ids and no
`run started` record, so those calls have no recorded actor. Correlation and trace ids are chosen by the caller: they
link a call to a conversation but do not prove who made it. CloudTrail shows the tool functions' roles, not the
operator.

## Transaction Search

AgentCore Runtime spans, Gateway and Memory traces, and online evaluations need CloudWatch Transaction Search, which
is an **account-and-region setting**: once on, X-Ray spans are ingested into CloudWatch Logs (`aws/spans`) and billed
as such for every application in the account. FAST Channels therefore leaves it off by default.

| `observability.transaction_search` | Effect |
|---|---|
| `off` (default) | No spans: the Runtime sets `OTEL_TRACES_EXPORTER=none`; logs and metrics are unaffected |
| `enable` | Deploys `FastChannels-AccountBaseline` (Transaction Search with 1 % indexing, the free tier, and its log resource policy), then trace deliveries for Gateway and Memory, span export from the Runtime and online evaluations; message content is captured unless `capture_prompts` is `false` |
| `existing` | Transaction Search is already on in this account and region: same as `enable` without the baseline stack |

The baseline stack is termination protected and its resources are retained, so deleting a deployment never turns
Transaction Search off for other workloads; delete the stack deliberately to do so. The traced configuration has been
synthesised and unit-tested but not deployed end to end in v3.0.
