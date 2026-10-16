# Observability and traceability

Every service in FAST Channels logs to CloudWatch Logs (CloudFront to S3) with a retention, traces where the service
supports tracing, and one correlation id threads an asset, a schedule or a viewing session through all of them. This
page lists what logs where, the correlation contract, the saved queries and `scripts/trace.py`, and the dashboard,
alarms and notifications.

- [Logging and tracing matrix](#logging-and-tracing-matrix)
- [Correlation contract](#correlation-contract)
- [Tracing an id](#tracing-an-id)
- [Dashboard and alarms](#dashboard-and-alarms)
- [Metrics](#metrics)
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
| CloudFront | Standard logs v2, Parquet with Hive partitions, CMCD fields; optionally JSON in an Infrequent Access log group | Logs bucket `AWSLogs/aws-account-id=<acct>/CloudFront/…`; `/aws/vendedlogs/cloudfront/<prefix>` | CloudFront metrics (us-east-1) |
| S3 | Server access logs of the ingest and media buckets | Logs bucket `s3/ingest/`, `s3/media/` | |
| AgentCore Runtime | Application log records through ADOT (`otel-rt-logs` stream, with `correlation_id`, `actor`, `mode`, `model_id`); vended application and usage logs | `/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT`; `/aws/vendedlogs/bedrock-agentcore/<prefix>/runtime/<LOG_TYPE>` | OpenTelemetry spans with Transaction Search only |
| AgentCore Gateway, Memory | Vended application logs | `/aws/vendedlogs/bedrock-agentcore/<prefix>/<gateway\|memory>/APPLICATION_LOGS` | Trace deliveries with Transaction Search only |
| Cognito | Administrative API calls in CloudTrail; the Essentials feature plan does not export user-activity logs | CloudTrail | |

## Correlation contract

| Thread | Identifier | Set by | Appears in |
|---|---|---|---|
| Ingest | `correlation_id` = Step Functions execution name | The sidecar or `IngestRequest`, else derived from the S3 event | Every pipeline Lambda log line, the state machine logs, MediaConvert `UserMetadata.correlation_id` (job events), the library asset, the VOD source's `fast:correlation-id` tag, `Asset Ready`/`Asset Failed` |
| Agent conversation | AgentCore Runtime session id | The client (`X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`) | Runtime logs; the `correlation_id` of every tool call and its Lambda logs; ingest and kit executions started by tools; `plan_id` (`<channel>.<8 hex>`) |
| Program | Program name `<yyyymmddThhmmss>-<asset_id>` | `schedule___plan` | `PROGRAM#` item, Channel Assembly `CreateProgram`, the as-run log (`programName`), the EPG feeds |
| Ad break | SCTE-35 `SegmentationEventId` and UPID (`FST3` + content id) | `schedule___plan` (deterministic) | HLS `DATERANGE` `ID`/`SCTE35-OUT`, the ADS request (`evid=`, `upid=`), MediaTailor `MAKING_ADS_REQUEST`, the CloudFront log of the VAST request |
| Viewing session | MediaTailor session id = `[session.uuid]` = `aws.sessionId` = `awsSessionId` (SGAI) | MediaTailor | The ADS request (`sid=`), vended logs (`sessionId`), CloudFront logs (query string; `cmcd-session-id` when the player sends CMCD with that id) |
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
Parquet objects in S3 and are not searched; query them with Amazon Athena (Hive-partitioned Parquet), or give
`CdnAccessLogs` a `cloudwatch_retention` to get a Logs Insights copy that the script then includes.

### Saved Logs Insights queries

The Media stack saves four queries under `FastChannels-<prefix>/` in the CloudWatch console (Logs Insights → Saved
queries). Each takes one parameter and covers the media plane's log groups: the bus, MediaConvert, the pipeline's
state machines and functions, the ladder provisioner, MediaTailor's vended logs and the as-run log. The Operator
stack's logs (tool functions, Runtime) are not in them, because the Media stack cannot reference its dependants;
use `scripts/trace.py` for agent sessions.

| Query | Parameter | Use |
|---|---|---|
| `Trace asset` | `asset_id` | An asset id or ingest correlation id through the pipeline |
| `Trace session` | `session_id` | A MediaTailor playback session (ADS requests, fills, beacons, CDN) |
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
| `<prefix>-mediaconvert-job-errors` | A job on the deployment's queue errored |
| `<channel>-errors` | Channel Assembly 4xx + 5xx responses reach 10 in 5 minutes |
| `<channel>-manifest-requests` | Manifest requests stay at 80 % of the 50 TPS per-channel quota for 3 minutes |
| `<channel>-ads-errors` | ADS errors plus timeouts reach 5 in 5 minutes |
| `<channel>-fill-rate` | `Avail.FillRate` stays below 0.5 for 15 minutes |
| `<prefix>-channel-hours` | Running channel-hours in a day exceed 24 (a cost guard; raise it for production fleets) |
| `<prefix>-cdn-5xx` | CloudFront 5xx error rate (outside us-east-1: `<prefix>-<region>-cdn-5xx` in the `…-Edge-<region>` stack, with its own topic `<prefix>-<region>-alerts`) |

`Asset Failed`, `Ladder Failed` and failed `Autopilot Run` events are forwarded to the same topic as JSON. Set
`notifications.email` to subscribe an address, and confirm the subscription email (two of them outside us-east-1,
where the Edge stack has its own topic). Per-channel alarms exist for IaC channels only; agent channels appear in the
running-channel census and the channel-hours alarm.

Channel Assembly publishes no metric while a channel runs without viewers, so a `ChannelCensus` Express state machine
counts this deployment's `RUNNING` channels (by the `fast:app` tag) every 5 minutes into
`FastChannels/RunningChannels`, which the channel-hours alarm sums.

## Metrics

Namespace `FastChannels`:

| Metric | Dimensions | Emitted by |
|---|---|---|
| `AssetReady`, `AssetFailed` | `kind` | Ingest pipeline |
| `LadderProvisioningStarted`, `LadderReady`, `LadderFailed` | | Ladder provisioner |
| `ToolSucceeded`, `ToolFailed` | `tool` | Every agent tool call |
| `RunningChannels` | `App` | Channel census |

## Transaction Search

AgentCore Runtime spans, Gateway and Memory traces, and online evaluations need CloudWatch Transaction Search, which
is an **account-and-region setting**: once on, X-Ray spans are ingested into CloudWatch Logs (`aws/spans`) and billed
as such for every application in the account. FAST Channels therefore leaves it off by default.

| `observability.transaction_search` | Effect |
|---|---|
| `off` (default) | No spans: the Runtime sets `OTEL_TRACES_EXPORTER=none`; logs and metrics are unaffected |
| `enable` | Deploys `FastChannels-AccountBaseline` (Transaction Search with 1 % indexing, the free tier, and its log resource policy), then trace deliveries for Gateway and Memory, span export from the Runtime and online evaluations |
| `existing` | Transaction Search is already on in this account and region: same as `enable` without the baseline stack |

The baseline stack is termination protected and its resources are retained, so deleting a deployment never turns
Transaction Search off for other workloads; delete the stack deliberately to do so. The traced configuration has been
synthesised and unit-tested but not deployed end to end in v3.0.
