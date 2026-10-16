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
npx cdk deploy '**' -c fast-channels.agent.admin_email=ops@example.com
npx cdk deploy '**' -c fast-channels.channels.iac.0.tier=STANDARD
npx cdk synth  '**' -c 'fast-channels.channels.iac=[{"name": "{prefix}-news", "outputs": ["hls", "hls-avc"]}]'
```

An invalid configuration fails synth with one line per problem and its path. The tables below are generated from the
model's field documentation and list the model's defaults. The shipped `cdk.json` sets every field explicitly with
those values, plus one IaC channel, `{prefix}-demo` (`fast-demo` for the default prefix).

| Key | Type | Default | Description |
|---|---|---|---|
| `prefix` | str | `"fast"` | Name prefix of every resource and value of the `fast:app` tag. Change it to deploy several copies in one account and region. |
| `removal` | `destroy` or `retain` | `"destroy"` | `destroy` (demo: delete data with the stacks) or `retain` (production: keep data, enable deletion and termination protection). |
| `ladders.builtin` | list of str | `["standard"]` | Built-in ladders (`ladders/<name>.toml`), each deployed as a MediaConvert job template. Slate and house-ad kits are encoded at deploy time for the default ladder and every ladder an IaC channel uses. |
| `ladders.default` | str | `"standard"` | Ladder used when a request names none (ingest, agent proposals); one of `builtin`. |
| `channel_assembly.package_profiles` | list of str | `["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"]` | Rendition profiles registered as package configurations on every VOD source and slate (at most 5, the default per-source quota). Every channel's `outputs` must be a subset. |
| `channels.iac` | list of object | `[]` | Channels owned by CloudFormation (`owner = iac`), deployed with their playback configurations. |
| `channels.max_runtime_hours` | float or null | `4` | Hours after which a channel started by a tool is stopped automatically; `null` disables auto-stop. |
| `channels.max_agent_channels` | int | `3` | Maximum number of channels the agent may create (`<prefix>-agent-*`). |
| `delivery.content_domain` | str or null | `null` | CDN-neutral hostname for content segments and preconditioned ads (e.g. `media.example.com`). Requires `certificate_arn`; defaults to the CloudFront domain. |
| `delivery.certificate_arn` | str or null | `null` | ACM certificate in us-east-1 covering `content_domain` (CloudFront only accepts us-east-1 certificates). |
| `delivery.price_class` | `PriceClass_All` or `PriceClass_200` or `PriceClass_100` | `"PriceClass_All"` | CloudFront price class. |
| `delivery.cdns` | list of object | `[]` | Additional CDNs that chain to CloudFront; used to list alternative playback URLs. |
| `ads.mode` | `preconditioned` or `external_vast` | `"preconditioned"` | `preconditioned`: House ads encoded on each channel's ladder and served as a static VAST pod: MediaTailor never transcodes. `external_vast`: A third-party ad decision server; MediaTailor transcodes its creatives (single-codec outputs only). |
| `ads.url` | str | with `external_vast` | ADS URL; may contain documented MediaTailor dynamic variables such as `[session.uuid]`. |
| `agent.enabled` | bool | `true` | Deploy the operator plane (Cognito, agent tools, Gateway, AgentCore Runtime and Memory, console). |
| `agent.model` | str | `"{geo}.anthropic.claude-haiku-5-5"` | Default Bedrock model id or inference profile of the scheduler agent; one of `allowed_models`. `{geo}` stands for the deployment region's geography: `us` (US regions and ca-central-1), `eu` (Europe), `au` (ap-southeast-2) or `global` (elsewhere). |
| `agent.allowed_models` | list of str | `["{geo}.anthropic.claude-haiku-5-5", "{geo}.anthropic.claude-sonnet-5-5", "global.anthropic.claude-haiku-5-5", "global.anthropic.claude-sonnet-5-5"]` | Models the agent may be switched to at runtime (SSM ModelSpec); the runtime role may invoke only these. `{geo}` as in `model`; entries that resolve to the same id are kept once. |
| `agent.admin_email` | str or null | `null` | Email of the first `fast-admins` user (invited by Cognito). Omit to create users yourself. |
| `agent.machine_client` | bool | `false` | Create the confidential machine client (Cognito `client_credentials`) that integrations and the end-to-end tests use to call the agent and the Gateway; always created when a channel has an `autopilot_cron`. Anyone allowed `cognito-idp:DescribeUserPoolClient` can read its secret, so it is off by default. |
| `agent.console_dev_url` | str or null | `null` | Local console dev server (`http://localhost:5173/`) to accept as a sign-in and sign-out redirect, for `npm run dev` against this deployment. Off by default: with it, any process listening on that port of an operator's machine can receive their authorization codes. |
| `observability.transaction_search` | `off` or `enable` or `existing` | `"off"` | `enable` deploys the account-level FastChannels-AccountBaseline stack (an account-wide change: X-Ray spans are then billed as CloudWatch Logs); `existing` if it is already on in this account and region. |
| `observability.log_retention_days` | int | `7` | Retention of every log group, and expiry of the logs bucket (S3 access and CloudFront logs). |
| `observability.mediatailor_log_percent` | int | `100` | Share of MediaTailor sessions that emit vended logs (100 for demos, ~10 in production). |
| `observability.capture_prompts` | bool or null | `null` | Keep message content (operator prompts, the system prompt, tool results and model replies) in the agent runtime's OpenTelemetry logs and spans, where anyone who can read them sees every operator's conversation. Default: only when `transaction_search` is not `off`, because the online evaluations score that content. |
| `captions.enabled` | bool | `true` | Encode the caption tracks the built-in ladders define (`[[captions]]` in `ladders/<name>.toml`, IMSC1 text), with each asset's captions from its provided files, else from the source, else generated. `false` deploys the ladders without caption tracks (their caption-less versions). |
| `captions.required` | bool | `false` | Refuse to encode an asset whose spoken-language caption track would be empty (regulated distribution). |
| `captions.engine` | `elemental-inference` or `none` | `"elemental-inference"` | Generates the spoken language's captions when an asset has none: `elemental-inference` (AWS Elemental Inference smart subtitles) or `none` (the track stays empty, or the asset is refused when captions are required). |
| `captions.max_feeds` | int | `2` | Elemental Inference feeds the captioner uses at once (one per 10-minute chunk of a title). The account's `Active feeds per account` quota (2 by default, adjustable) is shared with every other use of the service. |
| `captions.dictionary_id` | str or null | `null` | Elemental Inference custom dictionary (names, brands, jargon) every captioner feed uses. |
| `notifications.email` | str or null | `null` | Email subscribed to alarms and asset/ladder failure events. |

`channels.iac[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Channel Assembly channel name. Must start with `<prefix>-`; `<prefix>-agent-` is reserved for channels the agent creates. `{prefix}` is replaced with the deployment prefix (the default is `{prefix}-demo`). |
| `ladder` | str | `"standard"` | Built-in ladder of every program, slate and house ad on this channel (one of `ladders.builtin`). |
| `outputs` | list of str | `["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"]` | Rendition profiles published as channel outputs (subset of `channel_assembly.package_profiles`). |
| `tier` | `BASIC` or `STANDARD` | `"BASIC"` | Channel tier; billed per running hour, and channels are never started automatically. |
| `autopilot_cron` | str or null | `null` | EventBridge Scheduler expression for the autopilot that keeps the schedule 24 h ahead, e.g. `cron(0 */6 * * ? *)`. Omit to schedule only by chat. |
| `brief` | object or null | `null` | Programming brief (ratings, genres, dayparts, break policy) enforced by the schedule planner. |

`delivery.cdns[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Short CDN name used in playback-URL listings, e.g. `cdn-b` (`cloudfront` is reserved for the primary). |
| `manifest_base` | str | required | Base URL this CDN serves MediaTailor manifests under (chains to CloudFront). |
| `content_base` | str | required | Base URL this CDN serves content segments under. |

<details>
<summary>Regenerate the tables after changing a field's documentation</summary>

```bash
uv run python - <<'EOF'
import json
import re
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from fast_channels.infra.config import CdnEndpoint, ChannelConfig, FastChannelsConfig


def text(doc):
    return re.sub(r"``([^`]+)``", r"`\1`", " ".join((doc or "").split()))


def kind(t):
    if get_origin(t) is UnionType:
        null = " or null" if NoneType in get_args(t) else ""
        return " or ".join(kind(a) for a in get_args(t) if a is not NoneType) + null
    if get_origin(t) is tuple:
        return f"list of {kind(get_args(t)[0])}"
    if issubclass(t, Enum):
        return " or ".join(f"`{m.value}`" for m in t)
    return "object" if issubclass(t, BaseModel) else t.__name__


def default(value):
    value = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return "required" if value is PydanticUndefined else f"`{json.dumps(value)}`"


def rows(model, path=""):
    for name, field in model.model_fields.items():
        key, t = f"{path}{name}", getattr(field.annotation, "__value__", field.annotation)  # resolve type aliases
        if get_origin(t) is Annotated:  # a discriminated union of sections (ads), one row per field
            variants = {get_args(v.model_fields["mode"].annotation)[0].value: v for v in get_args(get_args(t)[0])}
            modes = " or ".join(f"`{mode}`" for mode in variants)
            docs = " ".join(f"`{mode}`: {text(v.__doc__)}" for mode, v in variants.items())
            yield f"| `{key}.mode` | {modes} | {default(field.default.mode.value)} | {docs} |"
            for mode, v in variants.items():
                for sub, f in v.model_fields.items():
                    if get_origin(f.annotation) is not Literal:
                        yield f"| `{key}.{sub}` | {kind(f.annotation)} | with `{mode}` | {text(f.description)} |"
        elif isinstance(t, type) and issubclass(t, BaseModel) and not field.description:  # a section: its fields
            yield from rows(t, f"{key}.")
        else:
            yield f"| `{key}` | {kind(t)} | {default(field.default)} | {text(field.description)} |"


for title, model in [("", FastChannelsConfig), ("channels.iac[]", ChannelConfig), ("delivery.cdns[]", CdnEndpoint)]:
    heading = [f"`{title}`:", ""] if title else []
    print(*heading, "| Key | Type | Default | Description |", "|---|---|---|---|", *rows(model), "", sep="\n")
EOF
```

</details>

A programming brief (`brief`) has `ratings`, `genres_allow`, `genres_deny`, `dayparts` (`{start, end, ratings,
genres}`, UTC `HH:MM`, may wrap midnight), a `break_policy` and free-text `notes` for the agent. The break policy has
`cadence_minutes` (12), `min_gap_minutes` (half the cadence unless set), `max_breaks` per program (unlimited),
`slate_gops` (30), `start_margin_seconds` (60), `end_margin_seconds` (30) and `min_program_minutes` (3); see
[Programs and ad breaks](channel-assembly.md#programs-and-ad-breaks). Operators can replace a brief at runtime with
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
- `{geo}` in model ids resolves at synth time: `us.` in US regions and ca-central-1, `eu.` in Europe, `au.` in
  ap-southeast-2, `global.` elsewhere (APAC regions have no `apac.` profile of the 5.5 models), so no region needs a
  model override.
- An IaC channel with an `autopilot_cron` also creates the autopilot's Cognito machine client, its Cedar permits,
  schedules, function and alarms; without one, none of them exists ([agent.md](agent.md#autopilot)).

## Deploy, update and destroy

### First deployment

See the [README quick start](../README.md#quick-start) for the prerequisites. In short:

```bash
make install          # uv workspace, CDK CLI, console dependencies
make model-access     # check Bedrock access to the agent's models (once per account)
make deploy           # builds console/dist, synthesizes (cdk-nag), deploys every stack
```

The first deployment takes about 20 minutes, unattended; the measured one took 20 min 20 s (Data 2:48, Media
11:18 including the default ladder's kit, Operator 5:29). Most of it is the CloudFront distributions, the AgentCore
resources and the ladder kit, whose encodes run inside the deployment (a `LadderKit` custom resource waits up to an
hour for them). `make deploy PREFIX=news` deploys a second, independent copy as `FastChannels-news-*`. Other
overrides go to the CDK CLI directly, for example
`npx cdk deploy '**' --require-approval never -c fast-channels.notifications.email=ops@example.com`.

`make synth` and `make diff` show what would change. Every synth runs cdk-nag (AWS Solutions and Serverless packs) and
fails on unacknowledged findings.

### Updates

- Redeploying with an unchanged ladder and brand stills takes seconds for the kit: it is idempotent by fingerprint.
- Changing an IaC channel (outputs, tier, ladder) while it is `RUNNING` fails fast before anything changes. Stop the
  channel, deploy, and start it again. (A `LinearChannel` built in code with `restart_on_update=True` is stopped
  and restarted by the deployment instead, also when a later failure rolls the update back.)
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
channels are stopped and drained by their own lifecycle resource. Then CloudFormation deletes the containers. A
destroy has not been timed for v3.0: plan for the CloudFront distributions (several minutes), about one second per VOD
source (five per kit plus one per encode) and, if an interrupted teardown orphaned programs, 6–8 minutes more.

Draining a channel deletes only the programs Channel Assembly can still hold: its schedule (14 days ahead) and the
library programs from a day ago on, never the whole program history. Each pass stops before its function times out
and reports what is left; the next poll, or the next `make destroy`, resumes where it stopped.

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
tokens 12 hours (rotated, revoked at sign-out). The console renews an access token once less than 10 minutes of it
remain, because the agent forwards it to the Gateway for every tool call of a run.

## Running channels

- **Deployments never start a channel.** Start one from the console's Channels view, by asking the agent, or with
  `channels___start_channel`. A started channel stops itself after `channels.max_runtime_hours` (default 4) through a
  one-time EventBridge Scheduler schedule in `<prefix>-auto-stop`, which is created before the channel starts;
  starting it again moves the stop time.
- **A channel can start at any time.** Every program is `ABSOLUTE` at its planned start (a not-before time), so
  each one airs at that time, within one segment. Starting a stopped channel late removes the programs whose start
  has passed, which would otherwise air late or replay: they are `SKIPPED` in the guide and listed in the tool's
  `skipped` output (the agent reports them), and filler plays until the next program
  ([channel-assembly.md](channel-assembly.md#starting-a-channel-late)).
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
| Captions | caption outputs free; a caption scan about $0.012 per content minute; generated captions about $0.18 per content minute (Elemental Inference $0.15 plus its proxy) | The scan only runs without provided captions, generation only without captions in the source ([captions.md](captions.md#cost)) |
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
| Storage | All buckets private (Block Public Access), SSE-S3, bucket-owner enforced, TLS-only policies, server access logs. Content manifests (`assets/*.m3u8`, `assets/*.mpd`) are denied to CloudFront, which stops casual direct playback of an asset from the CDN. It does not make ads mandatory: content segments are readable at predictable names (`assets/<asset>/<ladder>/main_<rendition>_<n>.cmfv`, and every SSAI playlist shows the prefix), so anyone can rebuild an ad-free playlist. Where that matters, protect `/assets/*` with CloudFront signed cookies or URLs, or a token check at the edge |
| Channel Assembly | Reads the media bucket with SigV4 only as the deployment's own source location, `sourceLocation/<prefix>-media` in the account. Channel policies admit only the channel's own playback configuration; anonymous requests get 403 |
| MediaConvert | The role trusts MediaConvert only for jobs on the deployment's queue (`aws:SourceArn` equals the queue ARN) and reads only the ingest bucket |
| Delivery | HTTPS only, TLS 1.2+ to origins, OAC to S3. MediaTailor has no CloudFront origin access control type, so its origins are public endpoints |
| Operators | Cognito, administrator-created users, three groups, Authorization Code with PKCE, optional TOTP MFA, rotating and revocable refresh tokens. The console client redirects only to the deployed console; a local dev server is accepted only with `agent.console_dev_url`. The console is served with a strict Content-Security-Policy, HSTS, `nosniff` and `frame-ancestors 'none'` |
| Agent | The agent forwards the operator's own token; the Gateway authorises every call with Cedar in `ENFORCE` mode; approval-gated tools wait for a human; no role may change the Gateway or its policies (explicit `Deny`). The Gateway's service role trusts only this deployment's Gateway (`aws:SourceAccount`, `aws:SourceArn`), and the Runtime role may read no object of the CDK assets bucket but its own code |
| Autopilot credential | Exists only when an IaC channel has an `autopilot_cron`. Cognito returns the machine client's secret from `DescribeUserPoolClient`, which the AWS managed `ReadOnlyAccess` and `SecurityAudit` policies grant, so read-only access to the account is enough to mint autopilot tokens and plan and publish on the autopilot's channels. Deny `cognito-idp:DescribeUserPoolClient` on the user pool to every principal but the autopilot function's role (an SCP or permissions boundary) |
| Tools (IAM) | One least-privilege role per tool target; destructive MediaTailor actions only on `<prefix>-agent-*`; creation calls only with this deployment's `fast:app` request tag; item-level DynamoDB grants; the reaper reaches only `<prefix>-*` names |
| Model | Invocation limited to `agent.allowed_models`; the runtime-mutable ModelSpec cannot name other models or providers; no `temperature` |
| Prompt logging | Message content (operator prompts, the system prompt, tool results, model replies) stays out of the Runtime's logs and spans unless `observability.capture_prompts` is on, which it is by default only with `transaction_search` (online evaluations score that content). With it, anyone who can read `/aws/bedrock-agentcore/runtimes/<runtime id>-DEFAULT` reads every operator's conversation ([observability.md](observability.md#what-the-agent-runtime-records)) |
| Audit | The Gateway passes no caller identity to the tools. Every call of a write or destructive tool logs a `tool audit` record (tool, arguments and outcome, with the call's correlation id, Gateway request id and X-Ray trace id) in its function's log group, and the Gateway's `APPLICATION_LOGS` record each request; for agent runs the Runtime's `run started` record names the actor of the same correlation id (the session id). Correlation and trace ids are chosen by the caller, and console calls straight to the Gateway (Start, Stop) carry no actor, so these records link calls to conversations but do not prove who made them ([observability.md](observability.md#audit-trail)) |
| Alerts | EventBridge publishes failure events to `<prefix>-alerts` through a rule-scoped role; the topic admits no service principal |
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
- With an autopilot, deny `cognito-idp:DescribeUserPoolClient` on the user pool to everyone but the autopilot's role
  (see [Security model](#security-model)).
- Keep message content out of the Runtime's logs outside debugging sessions (with Transaction Search, set
  `observability.capture_prompts` to `false` unless you use the online evaluations), and limit `logs:GetLogEvents`
  on the Runtime's log group.
- Protect `/assets/*` with CloudFront signed cookies or URLs if viewers must not be able to skip ads.
- Review `channels.max_agent_channels` and `channels.max_runtime_hours`.
- Raise the Channel Assembly manifest-request quotas before launch and set `channel_manifest_tps_quota` on
  `MediaObservability` to match.
- Enable Amazon Bedrock model invocation logging and keep CloudTrail on. Invocation logs hold the prompts and replies
  too, so restrict who can read their destination.

## Troubleshooting

### Deployment and teardown

| Symptom | Cause | Fix |
|---|---|---|
| Synth fails: the console build is missing | `console/dist` does not exist | `make console` (`make deploy` builds it) |
| `invalid FAST Channels configuration:` | A field or cross-field rule | The message names each path; see [configuration](#configuration-reference) |
| `npx cdk deploy --all` fails with `No stack found in the main cloud assembly` (and `diff --all` shows nothing) | The stacks are inside a CDK Stage, which `--all` does not select | Select them by pattern, as the Makefile does: `npx cdk deploy '**'` |
| Region rejected at synth | Channel Assembly, MediaConvert or AgentCore missing there | Use a listed region, or `agent.enabled=false` for media only |
| `LadderKit` fails | One kit encode failed; the stack rolls back | Check `/aws/vendedlogs/states/<prefix>-ladder-provisioner` and `make trace ID=<execution>`; redeploying resumes, items left `INGESTING` by aborted encodes are taken over |
| A kit encode fails with `video_description [1]: Invalid frame rate: 0/0` | The MediaConvert role was only seconds old and could not read the still yet (also the symptom of a wrong role trust condition) | Redeploy a few minutes later |
| Update fails: channel is `RUNNING` | The update guard protects live channels | Stop the channel, deploy, start it |
| Deletion fails at `RuntimeResourceReaper` | A non-transient error on its first, synchronous attempt | Fix the error in the stack events and run `make destroy` again |
| Deletion fails at a channel's lifecycle resource: `deleted <n> programs of <channel>, <m> remain` | The drain ran out of time before every program was deleted | Run `make destroy` again; it resumes with the programs that remain |
| Deletion fails on the source location or a slate | A runtime resource created outside the tools (e.g. a channel created by hand uses the filler slate) | Delete that resource, or run `scripts/cleanup.py --apply`, then destroy again |

### Ingest

| Symptom | Cause | Fix |
|---|---|---|
| `Asset Failed` with `IngestRefusedError … crosses the 50/60 Hz families` | 25 or 50 fps source on a 29.97 fps ladder | Use a ladder of the source's family, or set `allow_frame_rate_conversion` |
| `Asset Failed` with `InvalidIngestRequest` | The key contains whitespace, or the sidecar is invalid | Rename the object; check the sidecar against `contracts/ingest-request.schema.json` |
| `Asset Failed` with `IngestRefusedError … is already READY with <ladder version>` | The asset is already encoded with that ladder (a re-upload of the same key, or a request with the same `asset_id`); encoding it again would overwrite the media its VOD source serves | Upload under a new key, or pass a new `asset_id`; to use another ladder, request that `ladder_version` |
| `Asset Failed` with `DuplicateCorrelationId` | A new request reused a correlation id that already named an ingest (execution names are unique for 90 days), e.g. an unchanged sidecar | Send it again with a new `correlation_id` |
| Ingest `FAILED` with `3403: Access to https://… is forbidden` | The HTTPS origin refuses MediaConvert's fetcher (the Blender Foundation's servers do) | Copy the file into the ingest bucket and ingest the `s3://` object |
| An execution ends at `AlreadyIngesting` | The same file was uploaded again while it was encoding | Nothing to do: the first execution continues |
| `library___ingest` returns `CONFLICT` (`ExecutionAlreadyExists`) | The same asset was re-ingested in the same agent session | Use another asset id, or a new conversation |
| Ingest `FAILED` at Finalize (`InvalidOutputsError`) | A manifest is missing or its signature differs from the ladder | Inspect the job in MediaConvert; re-ingest |
| Colour-space warnings 230001/230005 | Untagged source | Harmless |
| `Asset Failed` with `IngestRefusedError … captions are required` | `captions.required` and the spoken-language track would be empty: no file, none in the source, and no generated captions (no engine, an unsupported language, or a captioner failure) | Provide a caption file, or check `CaptionGenerationFailed` and the captioner's execution (`/aws/vendedlogs/states/<prefix>-captioner`); `UnsupportedLanguageError` means smart subtitles has no such language |
| `IngestRefusedError … caption file … must be in the ingest bucket` | An `s3://` caption file in another bucket | Copy it into the ingest bucket, or serve it over https |
| A provided SCC file gives an empty track | Its timecodes start after the content (authored against a start timecode the source lacks) | Set `offset_ms` on the file (for example `-3600000` for `01:00:00;00`) |
| Captions show `<font>` or `<b>` text | SRT markup, which MediaConvert does not interpret | Provide WebVTT or TTML instead |
| Captions play until the first ad break or programme change, then stop (hls.js players) | An unpatched hls.js 1.7.x: a subtitle fragment that fails to parse, or arrives before the video's timestamps of its discontinuity, stalls subtitle loading | Apply the console's two hls.js fixes (`hlsSubtitleFixes` in `console/vite.config.ts`) to the player ([captions.md](captions.md#through-channel-assembly-ad-insertion-and-players)) |
| `InvalidOutputsError … cannot be re-cut on the video's segments` | A caption segment Finalize cannot read (not one IMSC1 sample per segment, or timing other than `begin`/`end` on `<p>`) | Inspect the job's caption outputs; re-ingest |

### Schedules and playback

| Symptom | Cause | Fix |
|---|---|---|
| `PLAN_STALE` on publish | The live schedule changed since planning (another publish, a removal, a late channel start, aired programs) | Plan again |
| `CONFLICT` (retryable) on publish | Another publish holds the channel's lease | Retry after it finishes |
| Publish fails with `published <k> of <n> programs of <plan id>, then: …` | A program could not be created (a Channel Assembly error, or the function ran out of time) after the first `k` were published; they are in the library and the guide | Publish the same `plan_id` again to add the rest, or remove the published ones with `schedule___remove_programs` |
| `channels___start_channel` returns `skipped` programs, `SKIPPED` in the guide | The channel started after their planned start; they were removed so that they would not air late or replay | Expected: the rest airs at its planned times. Plan a block to fill the gap if it is long |
| `channels___start_channel` fails retryably with `removed <k> of the <n> programs … ran out of time` | Too many missed programs to remove in one call; the channel was not started | Call it again: it removes the rest, then starts the channel |
| A program starts a few seconds late | `ABSOLUTE` starts snap to the next segment boundary of the filler or of the program before (at most one segment) | Expected |
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
| Console sign-in fails locally (`redirect_mismatch`) | The console client accepts a local dev server only when the deployment allows it | Deploy with `-c fast-channels.agent.console_dev_url=http://localhost:5173/` and use `npm run dev` on port 5173 |
| A tool call shows "Declined" (`DECLINED`) or "Failed" (`TOOL_ERROR`) | The operator rejected the approval, or the call never reached a tool (Cedar denial, Gateway or MCP error) | The result's message says which; a Cedar denial names the policy (`<prefix>_tools_<policy>`) |
| Invitation emails do not arrive | Cognito's default sender quota | Use SES, or create users with a known temporary password |
