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
npx cdk synth  '**' -c 'fast-channels.channels.iac=[{"name": "{prefix}-news", "outputs": ["hls"]}]'
```

An invalid configuration fails synth with one line per problem and its path. The tables below are generated from the
model's field documentation and list the model's defaults. The shipped `cdk.json` sets every field explicitly with
those values, plus one IaC channel, `{prefix}-demo` (`fast-demo` for the default prefix). A
[stage file](#stages) holds the overrides of one named deployment.

The `modules` section holds optional modules for ad monetization, which are being built on the
`feature/agentic-monetization` branch. Every module is off by default, and with every module off the stacks are
exactly v3.0.0's, which snapshot tests of every template check (`infra/tests/test_snapshots.py`). A module's
settings are validated from the start, but enabling a module changes nothing until its code is merged. Merged so
far: `models` (the per-task model registry, [agent.md](agent.md#model-registry)).

| Key | Type | Default | Description |
|---|---|---|---|
| `prefix` | str | `"fast"` | Name prefix of every resource and value of the `fast:app` tag. Change it to deploy several copies in one account and region. |
| `removal` | `destroy` or `retain` | `"destroy"` | `destroy` (demo: delete data with the stacks) or `retain` (production: keep data, enable deletion and termination protection). |
| `ladders.builtin` | list of str | `["standard"]` | Built-in ladders (`ladders/<name>.toml`), each deployed as a MediaConvert job template. Slate and house-ad kits are encoded at deploy time for the default ladder and every ladder an IaC channel uses. |
| `ladders.default` | str | `"standard"` | Ladder used when a request names none (ingest, agent proposals); one of `builtin`. |
| `channel_assembly.package_profiles` | list of str | `["hls"]` | Rendition profiles registered as package configurations on every VOD source and slate (at most 5, the default per-source quota). Every channel's `outputs` must be a subset. |
| `channels.iac` | list of object | `[]` | Channels owned by CloudFormation (`owner = iac`), deployed with their playback configurations. |
| `channels.max_runtime_hours` | float or null | `4` | Hours after which a channel started by a tool is stopped automatically; `null` disables auto-stop. |
| `channels.max_agent_channels` | int | `3` | Maximum number of channels the agent may create (`<prefix>-agent-*`). |
| `delivery.content_domain` | str or null | `null` | CDN-neutral hostname for content segments and preconditioned ads (e.g. `media.example.com`). Requires `certificate_arn`; defaults to the CloudFront domain. |
| `delivery.certificate_arn` | str or null | `null` | ACM certificate in us-east-1 covering `content_domain` (CloudFront only accepts us-east-1 certificates). |
| `delivery.price_class` | `PriceClass_All` or `PriceClass_200` or `PriceClass_100` | `"PriceClass_All"` | CloudFront price class. |
| `delivery.cdns` | list of object | `[]` | Additional CDNs that chain to CloudFront; used to list alternative playback URLs. |
| `ads.mode` | `preconditioned` or `external_vast` | `"preconditioned"` | `preconditioned`: House ads encoded on each channel's ladder and served as a static VAST pod: MediaTailor never transcodes. `external_vast`: A third-party ad decision server; MediaTailor transcodes its creatives to the channel's variants. |
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
| `notifications.email` | str or null | `null` | Email subscribed to alarms and asset/ladder failure events. |
| `modules.ops.enabled` | bool | `false` | Deploy the ops module: a per-stage data lake (S3, Glue, Athena), a hash-chained ledger of changes and money events, account quota alarms and detectors, runbooks and a probe canary. |
| `modules.ops.lake_retention_days` | int | `30` | Days after which lake objects expire. |
| `modules.ops.ledger_lock` | `off` or `governance` or `compliance` | `"governance"` | Object Lock mode of the ledger archive. `compliance` locks records even against the account root until their retention ends, so it needs `removal: retain`; use it only in production, after a retention review. |
| `modules.ops.ledger_retention_days` | int | `30` | Object Lock retention of each ledger record; until it ends, `make destroy` cannot delete the archive (1 on development stages). |
| `modules.ops.canary` | bool | `true` | Run a synthetic playback canary on the probe playback configuration of each running channel. |
| `modules.models.enabled` | bool | `false` | Deploy the per-task model registry: one validated entry per LLM task, each with its own IAM role, application inference profile and spend cap. Entries choose only among `agent.allowed_models`. |
| `modules.models.tasks` | map of str to object | `{"scheduler.plan": {"model": "{geo}.anthropic.claude-haiku-5-5", "route": "bedrock", "effort": {"chat": "low", "plan": "medium"}, "thinking": "adaptive", "max_tokens": 16000, "data_class": "operational", "daily_cap_usd": 5.0, "eval": "haiku-5-5-adaptive-16k"}}` | Registry entries by task name (dot-separated lowercase words); `scheduler.plan` is the scheduler agent. |
| `modules.models.budgets_action` | bool | `false` | Add an AWS Budget per task role with a `daily_cap_usd` whose action, once the task's spend in the month reaches 31 days of the cap, denies the role's invocations of the task's inference profile (Budgets actions accept no daily budget). Needs `notifications.email` (AWS Budgets emails it when the action runs) and the `fast:task` cost allocation tag activated in the payer account. |
| `modules.demand.enabled` | bool | `false` | Deploy programmatic demand: the Guidance for Deploying a Prebid Server on AWS as its own app at a pinned tag, with the repository's overlays and Prebid Server module, the guidance's bidder simulator in demo mode, census-gated scaling and an origin rule that admits only this stage's MediaTailor requests. |
| `modules.demand.mode` | `own_book` or `demo` | `"demo"` | `own_book`: own-book bids only (no simulator); `demo`: the own book plus the guidance's bidder simulator, every number labelled simulated. |
| `modules.demand.guidance_tag` | str | `"v1.4.0"` | Tag of the guidance, pinned with its commit and the hash of every file the overlays patch. |
| `modules.demand.pbs_version` | `3.43.0` or `4.4.0` | `"3.43.0"` | Prebid Server (Java) release of the image. |
| `modules.demand.simulator` | `guidance_stack` or `lambda` | `"guidance_stack"` | `guidance_stack`: the guidance's simulator stack (its own VPC with two NAT gateways); `lambda`: the same handler as a Lambda target behind an internal load balancer in the Prebid Server VPC, which needs no VPC of its own. |
| `modules.demand.tasks.min` | int | `2` | Prebid Server tasks while any channel runs (the tier minimum); 0 while none does. |
| `modules.demand.tasks.max` | int | `4` | Most Prebid Server tasks (the ECS service maximum). |
| `modules.demand.server_instances` | int | `2` | Prebid Server `server.http.server-instances`: one event loop per task vCPU. |
| `modules.demand.dayparts` | list of object | `[]` | Scheduled task minimums ahead of busy dayparts; target tracking covers the time between them. |
| `modules.demand.rate_limit_per_ip` | int | `20000` | Requests per client IP per 60 s above which the origin's web ACL blocks (an abuse cap, far above any MediaTailor egress address's rate). |
| `modules.pds.enabled` | bool | `false` | Deploy the Pod Decision Service: the per-break policy, pod selection and own book (house ads and direct lines) that the Prebid Server module calls, with reservations, delivery states and a decision record per break. |
| `modules.pds.policy_timeout_ms` | int | `50` | Timeout of the module's `/policy` call; on expiry the break is served from the own book alone. |
| `modules.pds.policy_group_timeout_ms` | int | `80` | Timeout of the hook group that holds the `/policy` call (at least `policy_timeout_ms`). |
| `modules.pds.select_timeout_ms` | int | `100` | Timeout of the module's `/select` call; on expiry the pod is built from house bids. |
| `modules.pds.select_group_timeout_ms` | int | `150` | Timeout of the hook group that holds the `/select` call (at least `select_timeout_ms`). |
| `modules.pds.provisioned_concurrency` | int | `2` | Provisioned concurrency per function while any channel runs; 0 while none does. |
| `modules.pds.demo_lines` | list of object | `[]` | Synthetic direct lines served by the own book in demo mode. |
| `modules.release.enabled` | bool | `false` | Deploy the Release Controller: each monetized channel gets a pair of playback configurations with MediaTailor Functions; a release goes to the idle one through a smoke test, is promoted by a gradual AppConfig pointer and is rolled back unattended on an alarm. |
| `modules.release.channels` | list of str | `[]` | IaC channels to monetize (names from `channels.iac`; `{prefix}` is replaced as there). Their channel policy also admits the sibling configurations `<channel>-p-?`. Adding a channel widens its policy once, and the deployment refuses that while the channel runs: stop it first. |
| `modules.release.strategy` | `demo` or `standard` or `large` | `"demo"` | `demo` (a simulated audience: 10% steps over 30 minutes), `standard` (up to 20,000 concurrent viewers: 10% steps over 3 hours) or `large` (exponential steps over 4 hours). |
| `modules.release.lab_channel` | bool | `true` | Create the stopped lab channel `<prefix>-lab`, started only to smoke-test a release while the monetized channel is stopped. |
| `modules.release.probe_sibling` | bool | `true` | Create a house-only probe playback configuration per channel, for canaries and smoke tests. |
| `modules.release.secret_rotation_days` | int | `90` | Days between rotations of the origin secret that MediaTailor sends to the demand origin. |
| `modules.crawl.enabled` | bool | `false` | Deploy show-level content metadata: a catalog context object per title, a cached PRE_ADS lookup by UPID and the OpenRTB `app.content` fields in each ad request, with a holdout that measures the uplift. |
| `modules.crawl.pre_ads` | bool | `true` | Map the cached PRE_ADS lookup on the monetized playback configurations. |
| `modules.crawl.holdout_percent` | int | `20` | Share of session-breaks that get no enrichment, the uplift measurement's control arm. |
| `modules.crawl.publisher_domain` | str or null | `null` | Your domain (e.g. `example.com`), named as the registry of your catalog's content ids in the catalog context objects and in OpenRTB `content.data[].cids`; unset, the content id travels in `content.id` only. |

`channels.iac[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Channel Assembly channel name. Must start with `<prefix>-`; `<prefix>-agent-` is reserved for channels the agent creates. `{prefix}` is replaced with the deployment prefix (the default is `{prefix}-demo`). |
| `ladder` | str | `"standard"` | Built-in ladder of every program, slate and house ad on this channel (one of `ladders.builtin`). |
| `outputs` | list of str | `["hls"]` | Rendition profiles published as channel outputs (subset of `channel_assembly.package_profiles`). |
| `tier` | `BASIC` or `STANDARD` | `"BASIC"` | Channel tier; billed per running hour, and channels are never started automatically. |
| `autopilot_cron` | str or null | `null` | EventBridge Scheduler expression for the autopilot that keeps the schedule 24 h ahead, e.g. `cron(0 */6 * * ? *)`. Omit to schedule only by chat. |
| `brief` | object or null | `null` | Programming brief (ratings, genres, dayparts, break policy) enforced by the schedule planner. |

`delivery.cdns[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | str | required | Short CDN name used in playback-URL listings, e.g. `cdn-b` (`cloudfront` is reserved for the primary). |
| `manifest_base` | str | required | Base URL this CDN serves MediaTailor manifests under (chains to CloudFront). |
| `content_base` | str | required | Base URL this CDN serves content segments under. |

`modules.models.tasks.<task>`:

| Key | Type | Default | Description |
|---|---|---|---|
| `model` | str | required | Bedrock model id or inference profile; one of `agent.allowed_models`, with `{geo}` as there. |
| `route` | `bedrock` | `"bedrock"` | `bedrock`: the Converse API with the task's own role and application inference profile. |
| `effort` | `low` or `medium` or `high` or `max` or object | `{"chat": "low", "plan": "medium"}` | Reasoning effort: one level, or one per mode (`chat`, `plan`) for the scheduler agent. |
| `thinking` | `adaptive` or `disabled` | `"adaptive"` | Extended thinking; fixed per task, because changing it per call rewrites the prompt cache. |
| `max_tokens` | int | `16000` | Output token budget per model call. |
| `data_class` | `operational` or `viewer` | `"operational"` | `viewer` if the task's prompts carry viewer data, which keeps the model on a geographic inference profile (never `global.`); swapping such a task's model needs human approval. |
| `daily_cap_usd` | float or null | `null` | Daily model spend at which the task's token counter refuses further calls (about ten times expected use); `null` sets no cap. |
| `eval` | str or null | `null` | Key of the passing evaluation record that qualifies `model` for this task. |

`modules.demand.dayparts[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `cron` | str | required | Application Auto Scaling `cron(minutes hours day-of-month month day-of-week year)` expression (UTC); fire it at least 10 minutes before the daypart's first break. |
| `min` | int | required | Task minimum from then on (between `tasks.min` and `tasks.max`). |

`modules.pds.demo_lines[]`:

| Key | Type | Default | Description |
|---|---|---|---|
| `id` | str | required | Line id, unique among `demo_lines`. |
| `advertiser` | str | required | Advertiser name (decision records and reports). |
| `adomain` | str | required | Advertiser domain (OpenRTB `adomain`); pods never repeat one. |
| `cat` | list of str | `[]` | IAB categories of the line's ads (OpenRTB `cat`). |
| `creatives` | list of str | required | Creative ids (`crid`) of the demo creatives the line serves. |
| `cpm` | decimal | required | Price per 1,000 impressions, US dollars (simulated). |
| `budget` | decimal | required | Total budget, US dollars (simulated); the line stops once reservations reach it. |
| `caps.per_session` | int or null | `null` | Most impressions of the line per playback session; `null`: uncapped. |
| `tier0` | bool | `false` | A guaranteed line: placed first in a pod, ahead of any bid. |

<details>
<summary>Regenerate the tables after changing a field's documentation</summary>

```bash
uv run python - <<'EOF'
import json
import re
from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Literal, get_args, get_origin

from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_jsonable_python

from fast_channels.infra.config import (
    CdnEndpoint,
    ChannelConfig,
    DemandDaypart,
    DemoLine,
    FastChannelsConfig,
    ModelTask,
)


def text(doc):
    return re.sub(r"``([^`]+)``", r"`\1`", " ".join((doc or "").split()))


def kind(t):
    if get_origin(t) is UnionType:
        null = " or null" if NoneType in get_args(t) else ""
        return " or ".join(kind(a) for a in get_args(t) if a is not NoneType) + null
    if get_origin(t) is tuple:
        return f"list of {kind(get_args(t)[0])}"
    if get_origin(t) is dict:
        return f"map of {kind(get_args(t)[0])} to {kind(get_args(t)[1])}"
    if issubclass(t, Enum):
        return " or ".join(f"`{m.value}`" for m in t)
    return "object" if issubclass(t, BaseModel) else t.__name__.lower()


def default(field):
    if field.is_required():
        return "required"
    value = field.get_default(call_default_factory=True)
    if field.default_factory:  # a factory may return raw values, validated like a value from cdk.json
        value = TypeAdapter(field.annotation).validate_python(value)
    return f"`{json.dumps(to_jsonable_python(value))}`"


def rows(model, path=""):
    for name, field in model.model_fields.items():
        key, t = f"{path}{name}", getattr(field.annotation, "__value__", field.annotation)  # resolve type aliases
        if get_origin(t) is Annotated:  # a discriminated union of sections (ads), one row per field
            variants = {get_args(v.model_fields["mode"].annotation)[0].value: v for v in get_args(get_args(t)[0])}
            modes = " or ".join(f"`{mode}`" for mode in variants)
            docs = " ".join(f"`{mode}`: {text(v.__doc__)}" for mode, v in variants.items())
            yield f"| `{key}.mode` | {modes} | `{json.dumps(field.default.mode.value)}` | {docs} |"
            for mode, v in variants.items():
                for sub, f in v.model_fields.items():
                    if get_origin(f.annotation) is not Literal:
                        yield f"| `{key}.{sub}` | {kind(f.annotation)} | with `{mode}` | {text(f.description)} |"
        elif isinstance(t, type) and issubclass(t, BaseModel) and not field.description:  # a section: its fields
            yield from rows(t, f"{key}.")
        else:
            yield f"| `{key}` | {kind(t)} | {default(field)} | {text(field.description)} |"


tables = [
    ("", FastChannelsConfig),
    ("channels.iac[]", ChannelConfig),
    ("delivery.cdns[]", CdnEndpoint),
    ("modules.models.tasks.<task>", ModelTask),
    ("modules.demand.dayparts[]", DemandDaypart),
    ("modules.pds.demo_lines[]", DemoLine),
]
for title, model in tables:
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
- Cross-field validation: channel outputs ⊆ `package_profiles`; `external_vast` requires a URL with documented ADS
  variables only; the region must offer MediaTailor Channel Assembly and MediaConvert (and AgentCore when
  `agent.enabled`); geographic inference profiles must be callable from the region; `log_retention_days` must be a
  CloudWatch retention value.
- Module rules: an enabled `demand` needs `ops`; `pds` needs `ops` and `demand`; `release` needs `ops`, `demand`
  and `pds`; `crawl` needs `pds` and `release`. `release.channels` ⊆ the IaC channel names (after `{prefix}` is
  replaced). With `release` on and `lab_channel` true, no IaC channel may be called `<prefix>-lab`. With `ops` on,
  `ledger_lock: compliance` needs `removal: retain`. With `models` on, every task's model is one of
  `agent.allowed_models` and callable from the region, a task with `data_class: viewer` never names a `global.`
  profile (also when `{geo}` resolves to `global`), with `agent.enabled` the registry holds the scheduler agent's
  `scheduler.plan`, every task's `eval` names a passing eval record for its model and settings
  (`evals/records/<task>/<key>.json`, [agent.md](agent.md#model-registry)), and `budgets_action` needs
  `notifications.email`. Within a module: `demand.tasks.min` ≤ `tasks.max`, with each
  daypart's `min` between them; each PDS hook group timeout at least its call's timeout; unique demo line ids and
  release channels. Unknown keys are refused at every level.
- Model-registry task names contain dots, which a dotted override path would split: override a task with an object,
  which is deep-merged, e.g. `-c 'fast-channels.modules.models.tasks={"scheduler.plan": {"max_tokens": 8000}}'`.
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

### Stages

A stage is a named deployment whose overrides live in a file, `stages/<name>.json`: a partial `fast-channels` block
that names its `prefix` and states only what differs from `cdk.json`. Each top-level key is passed as
`-c fast-channels.<key>=<JSON>` and deep-merged into `cdk.json`'s block, so `{"modules": {"ops": {"enabled": true}}}`
turns one module on and keeps the rest of `cdk.json`. `STAGE=<name>` selects a stage for `make synth`, `diff`,
`deploy` and `destroy` (as CDK context) and for `make seed` and `trace` (its prefix):

```bash
make synth STAGE=fastm                       # cdk-nag included
make deploy STAGE=fastm                      # FastChannels-fastm-Data, -Media, -Models, -Operator, -Crawl
make seed STAGE=fastm SEED_ARGS="--wait --yes"
make destroy STAGE=fastm
make deploy STAGE=fastci PREFIX=fastci-pr7   # the same stage under another prefix
uv run python scripts/stage_args.py context fastm   # the CDK arguments a stage becomes
```

A missing or malformed stage file stops `make` before any command runs. The repository ships two stages for the
modules' development: `fastm` and `fastci` (the end-to-end tests' default prefix), each with every module on.

A module with merged code adds its own stack after the ones it builds on, and may change theirs:

| Module | Adds | Changes |
|---|---|---|
| `ops` | `FastChannels-<prefix>-Ops`: the lake's Glue database `<prefix>_lake` (tables and views), the Athena workgroup `<prefix>-lake`, the lake copies of the IaC playback configurations' MediaTailor logs, the ledger writer with its Firehose stream, dead-letter queue and alarms, and the AppConfig application `<prefix>-fast` with environment `<prefix>` ([observability.md](observability.md#lake), [ledger](observability.md#ledger)) | Data: the lake bucket, the ledger table and the ledger archive (outputs `LakeBucketName`, `LedgerTableName`, `LedgerArchiveBucketName`). Media: the channel census also puts `Census Changed` events and keeps its last count in `/<prefix>/ops/running-channels` |

At most five stages can exist per account and region: each Media stack subscribes its as-run reconciler to the
account-shared log group `MediaTailor/Channel/AsRunLog`, and CloudWatch Logs allows five subscription filters per log
group (quota `L-87E7D306`, not adjustable).

With `modules.demand` on, `make deploy` deploys the repository's stacks only. The Prebid Server guidance, its own CDK
bootstrap and image follow with `make deploy-demand STAGE=<name>` ([demand.md](demand.md#deploy-and-destroy)), and
`make destroy-demand` removes them before `make destroy`.

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

- With `removal: destroy` (default) buckets are emptied and deleted, tables and Memory deleted. The Ops stack goes
  before Media and Data: its lake copies' deliveries first, then the Glue tables, views and workgroup (with its query
  history); the lake bucket (every version of every object) and the ledger table go with the Data stack.
- The ops module's ledger archive is **retained** whenever `modules.ops.ledger_lock` is `governance` or `compliance`
  (its records are under S3 Object Lock, and a ledger outlives the stack that wrote it). After `make destroy`,
  `uv run python scripts/ledger_verify.py purge --prefix <prefix>` lists it and `--apply` deletes the versions whose
  retention has passed and then the bucket; in governance mode `--apply --bypass-governance` deletes it at once
  (`s3:BypassGovernanceRetention` needed). Compliance-mode records cannot be deleted by anyone before their retention
  ends, which is why `compliance` needs `removal: retain`. Development stages use `ledger_retention_days: 1`.
- With `removal: retain` the stacks are termination protected and the Data stack's resources are retained; the
  reaper does nothing, so runtime resources stay too. To delete such a deployment, turn termination protection
  off, run `make destroy` (the Media stack stops at `DELETE_FAILED` while its source location still holds VOD
  sources), then `scripts/cleanup.py --apply` and `make destroy` again.
- Never deleted with a deployment: the `FastChannels-AccountBaseline` stack (Transaction Search), the account-shared
  as-run log group `MediaTailor/Channel/AsRunLog`, MediaConvert job history, CDK bootstrap assets and, with the ops
  module, a locked ledger archive (above).
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
| MediaTailor | Ad Insertion requests: manifests, session initialization, tracking, `/v1/segment` redirects | 10,000 per second per account | Binds first, at about 37,000 concurrent SSAI viewers at 70 %; see [capacity](delivery-and-multi-cdn.md#capacity) |
| Channel Assembly | Channel manifest requests | 50 TPS per channel, 400 per account | SSAI reuses responses across sessions: about 9 TPS per channel at 1,600 viewers. Binds only with many busy channels; see [capacity](delivery-and-multi-cdn.md#capacity) |
| Channel Assembly | Programs per channel; outputs per channel; package configurations per source | 400; 5; 5 | See [channel-assembly.md](channel-assembly.md#quotas-and-rate-limits) |
| Channel Assembly | Channels per account; VOD sources per source location | 100; 1,000 | |
| MediaConvert | Concurrent jobs of the deployment's on-demand queue | 20 (`IngestPipeline(concurrent_jobs=…)`) | Further uploads queue |
| MediaConvert | Custom job templates per region | 100 | One per ladder version, built-in or agent-provisioned |
| Step Functions | Execution names | Unique for 90 days | Correlation ids name executions |
| AgentCore Runtime | New sessions; active sessions | 25 TPS; 5,000 (us-east-1) | |
| Amazon Bedrock | Tokens and requests per minute per model | Account specific | Check before running many autopilots |
| CloudFront | Custom cache and response headers policies | 20 each per account | The distribution uses no custom cache policy and one response headers policy |
| CloudWatch Logs | Subscription filters per log group | 5 (`L-87E7D306`, not adjustable) | At most five deployments per account and region reconcile the shared as-run log |
| Cognito | Emails with the default sender | Low daily limit | Configure Amazon SES for production |

## Costs

us-east-1 list prices, rounded. Your bill is authoritative.

| Item | Price | Notes |
|---|---|---|
| Encoding, `standard` ladder | about $0.0375 per content minute ($2.25 per hour), MediaConvert Basic tier; $0.06 ($3.60 per hour) for a title whose cue points need ESAM (Professional tier) | [encoding.md](encoding.md#cost) |
| Ladder kit | about $0.25 (`standard`) | Once per ladder version |
| Sample content (`make seed`) | about $2.30 | About 39 minutes of open movies on `standard`; the three films' cue points fall between segment boundaries, so they encode with ESAM on the Professional tier. The script prints the estimate and asks first |
| Channel Assembly | $0.10 per running channel-hour (BASIC), $0.35 (STANDARD) | Billed only while `RUNNING`, per minute; auto-stop after 4 hours by default |
| Ad insertion | $0.25 per 1,000 ads (VOD rate) or $0.50 per 1,000 ads (live rate) | Which rate applies to Channel Assembly sources is not confirmed; budget for the live rate |
| CloudFront | Data transfer out and requests at CloudFront rates | One viewer-hour at about 3 Mbit/s is about 1.35 GB |
| S3 | Standard storage | At most about 5 GB per content hour on `standard` (sum of every variant's cap; QVBR usually lands well below) |
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
| Storage | All buckets private (Block Public Access), SSE-S3, bucket-owner enforced, TLS-only policies, server access logs. Content manifests (`assets/*.m3u8`) are denied to CloudFront, which stops casual direct playback of an asset from the CDN. It does not make ads mandatory: content segments are readable at predictable names (`assets/<asset>/<ladder>/main_<rendition>_<nnnnn>.ts`, and every SSAI playlist shows the prefix), so anyone can rebuild an ad-free playlist. Where that matters, protect `/assets/*` with CloudFront signed cookies or URLs, or a token check at the edge |
| Channel Assembly | Reads the media bucket with SigV4 only as the deployment's own source location, `sourceLocation/<prefix>-media` in the account. Channel policies admit only the channel's own playback configuration; anonymous requests get 403 |
| MediaConvert | The role trusts MediaConvert only for jobs on the deployment's queue (`aws:SourceArn` equals the queue ARN) and reads only the ingest bucket |
| Delivery | HTTPS only, TLS 1.2+ to origins, OAC to S3. MediaTailor has no CloudFront origin access control type, so its origins are public endpoints |
| Operators | Cognito, administrator-created users, three groups, Authorization Code with PKCE, optional TOTP MFA, rotating and revocable refresh tokens. The console client redirects only to the deployed console; a local dev server is accepted only with `agent.console_dev_url`. The console is served with a strict Content-Security-Policy, HSTS, `nosniff` and `frame-ancestors 'none'` |
| Agent | The agent forwards the operator's own token; the Gateway authorises every call with Cedar in `ENFORCE` mode; approval-gated tools wait for a human; no role may change the Gateway or its policies (explicit `Deny`). The Gateway's service role trusts only this deployment's Gateway (`aws:SourceAccount`, `aws:SourceArn`), and the Runtime role may read no object of the CDK assets bucket but its own code |
| Autopilot credential | Exists only when an IaC channel has an `autopilot_cron`. Cognito returns the machine client's secret from `DescribeUserPoolClient`, which the AWS managed `ReadOnlyAccess` and `SecurityAudit` policies grant, so read-only access to the account is enough to mint autopilot tokens and plan and publish on the autopilot's channels. Deny `cognito-idp:DescribeUserPoolClient` on the user pool to every principal but the autopilot function's role (an SCP or permissions boundary) |
| Tools (IAM) | One least-privilege role per tool target; destructive MediaTailor actions only on `<prefix>-agent-*`; creation calls only with this deployment's `fast:app` request tag; item-level DynamoDB grants; the reaper reaches only `<prefix>-*` names |
| Model | Invocation limited to `agent.allowed_models`; the runtime-mutable ModelSpec and model-registry entries cannot name other models or providers; no `temperature`. With `modules.models` each task's role may invoke only the task's application inference profile, registry tasks with viewer data stay on a geographic inference profile, and a daily spend cap per task stops a runaway |
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
- Restrict who may call `ssm:PutParameter` on `/<prefix>/agent/model-spec` (and `/<prefix>/models/tasks/*` with
  `modules.models`) and trim `agent.allowed_models`.
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
| An encode is billed on the Professional tier | The title has cue points between segment boundaries, so its job carries ESAM | Expected ([encoding.md](encoding.md#cue-points-and-esam)); cue points on the 6.006 s segment grid keep a title on the Basic tier |
| A cue point has no break offset, and Finalize logs a warning | The point lies beyond the end of the title, or no segment boundary shared by every variant lies within 100 ms of it | Check the cue points against the asset's duration and the segment boundaries in its media playlists |
| Colour-space warnings 230001/230005 | Untagged source | Harmless |

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
