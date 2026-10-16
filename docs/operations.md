# Operations

How to configure, deploy, run, secure and tear down FAST Channels, with the quotas and costs to plan for and a
troubleshooting playbook built from what went wrong during v3's live testing.

- [Configuration reference](#configuration-reference)
- [Deploy, update and destroy](#deploy-update-and-destroy)
- [Operator users](#operator-users)
- [Running channels](#running-channels)
- [Quotas and capacity](#quotas-and-capacity)
- [Costs](#costs)
- [Security model](#security-model)
- [Production hardening](#production-hardening)
- [Troubleshooting](#troubleshooting)

## Configuration reference

The deployment is configured by the `fast-channels` block of the CDK context in [`cdk.json`](../cdk.json), validated
by the pydantic model `FastChannelsConfig` (`infra/src/fast_channels/infra/config.py`). Override any field at synth
time with `-c fast-channels.<path>=<value>`; values are parsed as JSON where possible, list items are addressed by
index and objects are deep-merged:

```bash
npx cdk deploy 'FastChannels*/*' -c fast-channels.agent.admin_email=ops@example.com
npx cdk deploy 'FastChannels*/*' -c fast-channels.channels.iac.0.tier=STANDARD
npx cdk synth  'FastChannels*/*' -c 'fast-channels.channels.iac=[{"name": "fast-news", "outputs": ["hls", "hls-avc"]}]'
```

An invalid configuration fails synth with one line per problem and its path. The table below is generated from the
model's field documentation.

| Key | Type | Default | Description |
|---|---|---|---|
| `prefix` | str | `"fast"` | Name prefix of every resource and value of the `fast:app` tag. Change it to deploy several copies in one account and region. |
| `removal` | `destroy` or `retain` | `"destroy"` | `destroy` (demo: delete data with the stacks) or `retain` (production: keep data, enable deletion and termination protection). |
| `ladders.builtin` | list of str | `["standard"]` | Built-in ladders (`ladders/<name>.toml`) provisioned at deploy time with their slate and house-ad kit. |
| `ladders.default` | str | `"standard"` | Ladder used when a request names none (ingest, agent proposals); one of `builtin`. |
| `channel_assembly.package_profiles` | list of str | `["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"]` | Rendition profiles registered as package configurations on every VOD source and slate (at most 5, the default per-source quota). Every channel's `outputs` must be a subset. |
| `channels.iac` | list of object | `[]` | Channels owned by CloudFormation (`owner = iac`), deployed with their playback configurations. |
| `channels.max_runtime_hours` | float or null | `4` | Hours after which a channel started by a tool is stopped automatically; `null` disables auto-stop. |
| `channels.max_agent_channels` | int | `3` | Maximum number of channels the agent may create (`<prefix>-agent-*`). |
| `delivery.content_domain` | str or null | `null` | CDN-neutral hostname for content segments and preconditioned ads (e.g. `media.example.com`). Requires `certificate_arn`; defaults to the CloudFront domain. |
| `delivery.certificate_arn` | str or null | `null` | ACM certificate in us-east-1 covering `content_domain` (CloudFront only accepts us-east-1 certificates). |
| `delivery.price_class` | `PriceClass_All` or `PriceClass_200` or `PriceClass_100` | `"PriceClass_All"` | CloudFront price class. |
| `delivery.cdns` | list of object | `[]` | Additional CDNs that chain to CloudFront; used to list alternative playback URLs. |
| `ads` | object, `mode` = `preconditioned` or `external_vast` | `{"mode": "preconditioned"}` | `preconditioned`: house ads encoded on each channel's ladder and served as a static VAST pod; MediaTailor never transcodes. `external_vast`: a third-party ad decision server at `url`; MediaTailor transcodes its creatives (single-codec outputs only). |
| `agent.enabled` | bool | `true` | Deploy the operator plane (Cognito, agent tools, Gateway, AgentCore Runtime and Memory, console). |
| `agent.model` | str | `"us.anthropic.claude-haiku-5-5"` | Default Bedrock model id or inference profile of the scheduler agent; one of `allowed_models`. |
| `agent.allowed_models` | list of str | `["us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-5-5", "global.anthropic.claude-sonnet-5-5"]` | Models the agent may be switched to at runtime (SSM ModelSpec); the runtime role may invoke only these. |
| `agent.admin_email` | str or null | `null` | Email of the first `fast-admins` user (invited by Cognito). Omit to create users yourself. |
| `observability.transaction_search` | `off` or `enable` or `existing` | `"off"` | `enable` deploys the account-level FastChannels-AccountBaseline stack (an account-wide change: X-Ray spans are then billed as CloudWatch Logs); `existing` if it is already on in this account and region. |
| `observability.log_retention_days` | int | `7` | Retention of every log group, and expiry of the logs bucket (S3 access and CloudFront logs). |
| `observability.mediatailor_log_percent` | int | `100` | Share of MediaTailor sessions that emit vended logs (100 for demos, ~10 in production). |
| `notifications.email` | str or null | `null` | Email subscribed to alarms and asset/ladder failure events. |

`channels.iac[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Channel Assembly channel name. Must start with `<prefix>-`; `<prefix>-agent-` is reserved for channels the agent creates. |
| `ladder` | str | `"standard"` | Built-in ladder of every program, slate and house ad on this channel (one of `ladders.builtin`). |
| `outputs` | list of str | `["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"]` | Rendition profiles published as channel outputs (subset of `channel_assembly.package_profiles`). |
| `tier` | `BASIC` or `STANDARD` | `"BASIC"` | Channel tier; billed per running hour, and channels are never started automatically. |
| `autopilot_cron` | str or null | `null` | EventBridge Scheduler expression for the autopilot that keeps the schedule 24 h ahead, e.g. `cron(0 */6 * * ? *)`. Omit to schedule only by chat. |
| `brief` | object or null | `null` | Programming brief (ratings, genres, dayparts, break policy) enforced by the schedule planner. |

`delivery.cdns[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Short CDN name used in playback-URL listings, e.g. `cdn-b`. |
| `manifest_base` | str | required | Base URL this CDN serves MediaTailor manifests under (chains to CloudFront). |
| `content_base` | str | required | Base URL this CDN serves content segments under. |

A programming brief (`brief`) has `ratings`, `genres_allow`, `genres_deny`, `dayparts` (`{start, end, ratings,
genres}`, UTC `HH:MM`, may wrap midnight), `break_policy` (`cadence_minutes` 12, `min_gap_minutes` 6, `max_breaks`,
`slate_gops` 30) and free-text `notes` for the agent. Operators can replace it at runtime with
`schedule___update_brief`; a brief changed at runtime survives later deploys unless `cdk.json` changes it.

Notes:

- Kits are encoded at deploy time for the default ladder and for every ladder an IaC channel uses; other entries in
  `ladders.builtin` get their job template only.
- `delivery.cdns` adds per-CDN playback URLs to `channels___get_channel` and the console; it does not change
  what MediaTailor or Channel Assembly write ([multi-CDN](delivery-and-multi-cdn.md#multi-cdn-in-v30)).
- Cross-field validation: channel outputs ⊆ `package_profiles` and select at least one rendition of the channel's
  ladder; `external_vast` requires single-codec outputs and a URL with documented ADS variables only; the region must
  offer MediaTailor Channel Assembly and MediaConvert (and AgentCore when `agent.enabled`); geographic inference
  profiles must be callable from the region; `log_retention_days` must be a CloudWatch retention value.

## Deploy, update and destroy

### First deployment

See the [README quick start](../README.md#quick-start) for the prerequisites. In short:

```bash
make install          # uv workspace, CDK CLI, console dependencies
make model-access     # check Bedrock access to the agent's models (once per account)
make deploy           # builds console/dist, synthesizes (cdk-nag), deploys every stack
```

The first deployment takes about {{DEPLOY_MINUTES}} minutes, unattended. Most of it is the CloudFront distributions,
the AgentCore resources and the ladder kit, whose encodes run inside the deployment (a `LadderKit` custom resource
waits up to an hour for them). `make deploy PREFIX=news` deploys a second, independent copy as
`FastChannels-news-*`.

`make synth` and `make diff` show what would change. Every synth runs cdk-nag (AWS Solutions and Serverless packs) and
fails on unacknowledged findings.

### Updates

- Redeploying with an unchanged ladder and brand stills takes seconds for the kit: it is idempotent by fingerprint.
- Changing an IaC channel (outputs, tier, ladder) while it is `RUNNING` fails fast before anything changes. Stop the
  channel, deploy, and start it again.
- Changing a built-in ladder's TOML creates a new ladder version, job template and kit. Content encoded with the old
  version must be re-encoded before channels on the new version can air it.
- Changing `delivery.content_domain` re-encodes every kit (the content host is part of its fingerprint).
- The console is rebuilt and republished (`index.html` and `config.json` invalidated) on every deploy.

### Destroy

```bash
make destroy                       # or: make destroy PREFIX=news
```

Stack deletion runs the `RuntimeResourceReaper` first: it stops and deletes agent channels with their programs,
policies, playback configurations and log deliveries; deletes every VOD source of the deployment (1 TPS, retrying
sources that orphaned programs lock for 6–8 minutes); then runtime source locations and runtime job templates. IaC
channels are stopped and drained by their own lifecycle resource. Then CloudFormation deletes the containers. Expect
about {{DESTROY_MINUTES}} minutes.

- With `removal: destroy` (default) buckets are emptied and deleted, tables and Memory deleted.
- With `removal: retain` the stacks are termination protected and the Data stack's resources are retained; the
  reaper does nothing, so runtime resources stay too. To delete such a deployment, turn termination protection
  off, run `make destroy` (the Media stack stops at `DELETE_FAILED` while its source location still holds VOD
  sources), then `scripts/cleanup.py --apply` and `make destroy` again.
- Never deleted with a deployment: the `FastChannels-AccountBaseline` stack (Transaction Search), the account-shared
  as-run log group `MediaTailor/Channel/AsRunLog`, MediaConvert job history and CDK bootstrap assets.
- If the reaper fails on its first attempt (the error is shown in the stack events), run `make destroy` again; it
  resumes where it stopped.
- When the reaper cannot finish (a VOD source locked by orphaned programs, an interrupted destroy, a stack deleted
  with retained resources), `scripts/cleanup.py` finishes the job outside CloudFormation. It lists what is left
  by default and deletes it with `--apply` (agent channels, VOD sources, runtime source locations, runtime job
  templates, auto-stop schedules), retrying for up to 10 minutes while Channel Assembly keeps sources locked; it
  refuses while the stage is deployed and healthy unless `--force` is given. Run `make destroy` again afterwards.

  ```bash
  uv run python scripts/cleanup.py --prefix fast            # what is left
  uv run python scripts/cleanup.py --prefix fast --apply    # delete it
  ```

## Operator users

Users are created by administrators only and sign in with their email address through Cognito managed login. Either
set `agent.admin_email` before deploying (Cognito emails that person a temporary password for a `fast-admins`
account), or create users yourself:

```bash
POOL=$(aws cloudformation describe-stacks --stack-name FastChannels-Operator \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" --output text)
aws cognito-idp admin-create-user --user-pool-id "$POOL" --username jane@example.com \
  --user-attributes Name=email,Value=jane@example.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username jane@example.com --group-name fast-operators
```

Groups: `fast-viewers` (read), `fast-operators` (read and write), `fast-admins` (everything); see
[agent.md](agent.md#authorisation-cognito-groups-and-cedar). A user without a group can sign in but sees no tools.
Temporary passwords expire after 3 days; TOTP MFA is optional per user; access tokens last 60 minutes and refresh
tokens 12 hours (rotated, revoked at sign-out).

## Running channels

- **Deployments never start a channel.** Start one from the console's Channels view, by asking the agent, or with
  `channels___start_channel`. A started channel stops itself after `channels.max_runtime_hours` (default 4) through a
  one-time EventBridge Scheduler schedule in `<prefix>-auto-stop`; starting it again moves the stop time.
- Stop it the same way, or directly: `aws mediatailor stop-channel --channel-name fast-demo`.
- A running channel without a schedule plays its filler slate. Publish at least 10 minutes ahead of air time; later
  inserts work on a best-effort basis.
- Keep the schedule ahead with the autopilot (`autopilot_cron`) or by asking the agent to "extend fast-demo to a
  24-hour horizon".
- The `<prefix>-channel-hours` alarm fires when the running channels add up to more than 24 channel-hours in a day.

## Quotas and capacity

Defaults per account and region; request increases in Service Quotas before you need them.

| Service | Quota | Default | Notes |
|---|---|---|---|
| Channel Assembly | Channel manifest requests | 50 TPS per channel, 400 per account | Caps SSAI audiences at roughly 150–300 concurrent viewers per channel; see [capacity](delivery-and-multi-cdn.md#capacity) |
| Channel Assembly | Programs per channel; outputs per channel; package configurations per source | 400; 5; 5 | See [channel-assembly.md](channel-assembly.md#quotas-and-rate-limits) |
| Channel Assembly | Channels per account; VOD sources per source location | 100; 1,000 | |
| MediaConvert | Concurrent jobs of the deployment's on-demand queue | 20 (`IngestPipeline(concurrent_jobs=…)`) | Further uploads queue |
| MediaConvert | Custom job templates per region | 100 | One per ladder version, built-in or agent-provisioned |
| Step Functions | Execution names | Unique for 90 days | Correlation ids name executions |
| AgentCore Runtime | New sessions; active sessions | 25 TPS; 5,000 (us-east-1) | |
| Amazon Bedrock | Tokens and requests per minute per model | Account specific | Check before running many autopilots |
| CloudFront | Custom cache and response headers policies | 20 each per account | The distribution uses no custom cache policy and one response headers policy |
| CloudWatch Logs | Subscription filters per log group | 2 | At most two deployments per account and region reconcile the shared as-run log |
| Cognito | Emails with the default sender | Low daily limit | Configure Amazon SES for production |

## Costs

us-east-1 list prices, rounded. Your bill is authoritative.

| Item | Price | Notes |
|---|---|---|
| Encoding, `standard` ladder | about $0.56 per content minute ($33.60 per hour) | [encoding.md](encoding.md#cost) |
| Encoding, `uhd` ladder | about $1.36 per content minute | |
| Ladder kit | about $3.56 (`standard`), $8.63 (`uhd`) | Once per ladder version |
| Sample content (`make seed`) | about $22 | About 39 minutes of open movies on `standard`; the script prints the estimate and asks first |
| Channel Assembly | $0.10 per running channel-hour (BASIC), $0.35 (STANDARD) | Billed only while `RUNNING`, per minute; auto-stop after 4 hours by default |
| Ad insertion | $0.25 per 1,000 ads (VOD rate) or $0.50 per 1,000 ads (live rate) | Which rate applies to Channel Assembly sources is not confirmed; budget for the live rate |
| CloudFront | Data transfer out and requests at CloudFront rates | One viewer-hour at about 3 Mbit/s is about 1.35 GB |
| S3 | Standard storage | At most about 20 GB per content hour on `standard` (sum of every rendition's cap; QVBR usually lands well below) |
| AgentCore Runtime | $0.0895 per vCPU-hour and $0.00945 per GB-hour, per second | Only while a session is active; CPU is not billed during I/O wait; sessions idle out after 10 minutes |
| AgentCore Gateway and Policy | $0.005 per 1,000 tool invocations; $0.025 per 1,000 authorisations | |
| AgentCore Memory | Short-term events by GB ingested and stored; $0.75 per 1,000 preference records per month | Small for operator chat |
| Bedrock tokens | about $0.003–0.006 per schedule with Claude Haiku 5.5 | About ten times that with Sonnet 5.5 |
| CloudWatch | Log ingestion and storage; $0.10 per alarm per month; dashboards (3 free per account) | Vended MediaTailor logs scale with sessions: lower `mediatailor_log_percent` in production |
| EventBridge, Step Functions, Lambda, DynamoDB | Cents per month at demo scale | The channel census runs every 5 minutes |

What costs money while nobody watches: running channels (stop them), CloudWatch alarms and storage, S3 storage, and
the CloudFront, AgentCore and DynamoDB request charges of whatever still calls them. An idle deployment with stopped
channels costs a few dollars a month. A 24/7 BASIC channel adds about $73 a month before ads and delivery.

## Security model

| Layer | Control |
|---|---|
| Storage | All buckets private (Block Public Access), SSE-S3, bucket-owner enforced, TLS-only policies, server access logs. Content manifests (`assets/*.m3u8`, `assets/*.mpd`) are denied to CloudFront, so viewers cannot watch content without its ads |
| Channel Assembly | Reads the media bucket with SigV4 only for source locations named `<prefix>*` in the account. Channel policies admit only the channel's own playback configuration; anonymous requests get 403 |
| MediaConvert | The role trusts MediaConvert only for jobs on the deployment's queue (`aws:SourceArn` equals the queue ARN) and reads only the ingest bucket |
| Delivery | HTTPS only, TLS 1.2+ to origins, OAC to S3; MediaTailor OAC slot for when it exists |
| Operators | Cognito, administrator-created users, three groups, Authorization Code with PKCE, optional TOTP MFA, rotating and revocable refresh tokens. The console is served with a strict Content-Security-Policy, HSTS, `nosniff` and `frame-ancestors 'none'` |
| Agent | The agent forwards the operator's own token; the Gateway authorises every call with Cedar in `ENFORCE` mode; approval-gated tools wait for a human; no role may change the Gateway or its policies (explicit `Deny`) |
| Tools (IAM) | One least-privilege role per tool target; destructive MediaTailor actions only on `<prefix>-agent-*`; creation calls only with this deployment's `fast:app` request tag; item-level DynamoDB grants |
| Model | Invocation limited to `agent.allowed_models`; the runtime-mutable ModelSpec cannot name other models or providers; no `temperature` |
| Prompt injection | Tool results are treated as data by the prompt and never mined into long-term memory (`extractionMode=SKIP`); Cedar and approvals apply whatever the model decides |
| Code | No public Lambda function URLs; cdk-nag AwsSolutions and Serverless on every synth with per-construct acknowledgements and reasons |

Report vulnerabilities as described in [SECURITY.md](../SECURITY.md).

## Production hardening

- `removal: retain` for data and termination protection.
- `notifications.email` (confirm the subscription), and an AWS Budgets alert on the account.
- `observability.mediatailor_log_percent` around 10.
- A custom `delivery.content_domain` with an ACM certificate (TLS 1.2 2025 policy); geo restriction and AWS WAF where
  rights and threat model require them (both off by default, with the reasons recorded in the cdk-nag
  acknowledgements).
- Cognito: require MFA, send email through Amazon SES, consider the Plus feature plan for threat protection, and keep
  `fast-admins` small.
- Restrict who may call `ssm:PutParameter` on `/<prefix>/agent/model-spec` and trim `agent.allowed_models`.
- Review `channels.max_agent_channels` and `channels.max_runtime_hours`.
- Raise the Channel Assembly manifest-request quotas before launch and set `channel_manifest_tps_quota` on
  `MediaObservability` to match.
- Enable Amazon Bedrock model invocation logging and keep CloudTrail on.

## Troubleshooting

### Deployment and teardown

| Symptom | Cause | Fix |
|---|---|---|
| Synth fails: the console build is missing | `console/dist` does not exist | `make console` (`make deploy` builds it) |
| `invalid FAST Channels configuration:` | A field or cross-field rule | The message names each path; see [configuration](#configuration-reference) |
| `npx cdk deploy --all` fails with `No stack found in the main cloud assembly` (and `diff --all` shows nothing) | The stacks are inside a CDK Stage, which `--all` does not select | Select them by pattern, as the Makefile does: `npx cdk deploy 'FastChannels*/*'` |
| Region rejected at synth | Channel Assembly, MediaConvert or AgentCore missing there | Use a listed region, or `agent.enabled=false` for media only |
| `LadderKit` fails | One kit encode failed; the stack rolls back | Check `/aws/vendedlogs/states/<prefix>-ladder-provisioner` and `make trace ID=<execution>`; redeploying resumes, items left `INGESTING` by aborted encodes are taken over |
| A kit encode fails with `video_description [1]: Invalid frame rate: 0/0` | The MediaConvert role was only seconds old and could not read the still yet (also the symptom of a wrong role trust condition) | Redeploy a few minutes later |
| Update fails: channel is `RUNNING` | The update guard protects live channels | Stop the channel, deploy, start it |
| Deletion fails at `RuntimeResourceReaper` | A non-transient error on its first, synchronous attempt | Fix the error in the stack events and run `make destroy` again |
| Deletion fails on the source location or a slate | A runtime resource created outside the tools (e.g. a channel created by hand uses the filler slate) | Delete that resource, or run `scripts/cleanup.py --apply`, then destroy again |

### Ingest

| Symptom | Cause | Fix |
|---|---|---|
| `Asset Failed` with `IngestRefusedError … crosses the 50/60 Hz families` | 25 or 50 fps source on a 29.97 fps ladder | Use a ladder of the source's family, or set `allow_frame_rate_conversion` |
| `Asset Failed` with `InvalidIngestRequest` | The key contains whitespace, or the sidecar is invalid | Rename the object; check the sidecar against `contracts/ingest-request.schema.json` |
| An execution ends at `AlreadyIngesting` | The same file was uploaded again while it was encoding | Nothing to do: the first execution continues |
| `library___ingest` returns `CONFLICT` (`ExecutionAlreadyExists`) | The same asset was re-ingested in the same agent session | Use another asset id, or a new conversation |
| Ingest `FAILED` at Finalize (`InvalidOutputsError`) | A manifest is missing or its signature differs from the ladder | Inspect the job in MediaConvert; re-ingest |
| Colour-space warnings 230001/230005 | Untagged source | Harmless |

### Schedules and playback

| Symptom | Cause | Fix |
|---|---|---|
| `PLAN_STALE` on publish | The live schedule changed since planning (another publish, a removal, aired programs) | Plan again |
| `CONFLICT` (retryable) on publish | Another publish holds the channel's lease | Retry after it finishes |
| A block starts about a second late | `ABSOLUTE` starts snap to the next filler segment | Expected |
| Programs vanish from Channel Assembly | It deletes LINEAR programs 13–15 minutes after airing | Expected; the EPG and `schedule___get_schedule` read the library |
| `SCHEDULING_ERROR` alert | A program or slate lacks a source group of an output, or its renditions differ | `origins___list_alerts`; re-register package configurations; check ladder versions |
| Filler plays instead of programs | No program at that time, or a block started in the past | `schedule___get_schedule` shows the gaps; plan from now + 10 minutes |
| SSAI 504 for a few minutes after a channel change | Channel policy propagation | Wait |
| First ad break shows only slate | MediaTailor imports a new creative (`NEW_CREATIVE`) | Expected for the first break of each creative |
| SGAI does not show ads | Player without HLS Interstitials, or a blocked asset-list request to `interstitials.mediatailor…` | See [ad-insertion.md](ad-insertion.md#monitoring-and-troubleshooting) |

### Agent and console

| Symptom | Cause | Fix |
|---|---|---|
| The agent answers but every model call fails with `AccessDeniedException` | Model access not enabled in the account | `make model-access` (and the Anthropic use-case form) |
| `RUN_ERROR UNAUTHORIZED: Claim client_id value mismatch` | An ID token was sent instead of an access token | Send the Cognito access token |
| `RUN_ERROR ACCESS_DENIED` (HTTP 403) | Expired or invalid token | Sign in again |
| `RUN_ERROR GATEWAY_UNAVAILABLE`: the gateway lists no tools for this caller | The user is in no group (Cedar permits nothing), or the Gateway was deployed seconds ago | Add the user to a group; otherwise retry in a minute |
| `FORBIDDEN` (retryable) from `register_vod_source` | The BYO bucket policy is not applied or has not propagated | Apply the statements `create_source_location` returned; retry |
| The first autopilot run after a deploy failed | A `rate()` schedule fires at creation, when the new Gateway may not list tools yet | It is retried once; the next scheduled run works |
| Trace deliveries fail to deploy | X-Ray trace delivery needs Transaction Search | Set `observability.transaction_search` to `enable` or `existing` |
| Console sign-in loops locally | The local origin must be `http://localhost:5173/` | Use `npm run dev` on port 5173 |
| Invitation emails do not arrive | Cognito's default sender quota | Use SES, or create users with a known temporary password |
