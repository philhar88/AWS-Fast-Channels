# Optional modules: ad monetization and operations

FAST Channels v3.0.0 airs channels with preconditioned house ads. The optional modules add programmatic demand, a
decision service for every ad break, show-level metadata in ad requests, a release process for monetized playback
configurations, a per-task model registry, and the operations they need: a lake, a hash-chained ledger, detectors,
runbooks and a probe. They are being built on the `feature/agentic-monetization` branch.

Every module is off by default, behind `modules.<name>.enabled` in [`cdk.json`](../cdk.json). With every module off
a deployment is v3.0.0's, which snapshot tests of every template check (`infra/tests/test_snapshots.py`).

- [The modules](#the-modules)
- [Turning modules on](#turning-modules-on)
- [Deploy and destroy order](#deploy-and-destroy-order)
- [How each module fails safe](#how-each-module-fails-safe)
- [What the modules cost](#what-the-modules-cost)
- [Account-level limits and teardown residue](#account-level-limits-and-teardown-residue)

## The modules

| Module | What it does | Needs | Adds | Changes | Docs |
|---|---|---|---|---|---|
| `ops` | A per-stage lake for Athena over the CDN and MediaTailor logs. A hash-chained, Object-Locked ledger of unattended actions and money events. Account quota and hook alarms, a zero-ad-sessions detector and a Channel Assembly alerts poller. Break ids in the EPG and `aired_at` from the as-run log. Eight runbooks, AppConfig kill switches, and a probe canary that plays every running channel | — | `FastChannels-<prefix>-Ops` | Data: the lake bucket, the ledger table and its archive. Media: `Census Changed` events, `AdBreakStart` reconciliation and the break sweep, break ids in the EPG feed | [Lake](observability.md#lake), [ledger](observability.md#ledger), [runbooks, flags and the probe](observability.md#runbooks-flags-and-the-probe), [break reconciliation](channel-assembly.md#the-schedule-of-record-and-the-epg) |
| `models` | A per-task model registry over Bedrock Converse. Each LLM task gets a validated SSM entry, its own application inference profile (its own metrics and cost line), a daily spend breaker and an eval gate | — | `FastChannels-<prefix>-Models` | Operator: the scheduler agent reads its `scheduler.plan` entry and may invoke only its profile | [Model registry](agent.md#model-registry), [ADR-0029](adr/0029-per-task-model-registry.md) |
| `demand` | An OpenRTB auction behind the ad breaks: the AWS Prebid Server guidance at a pinned tag with the repository's overlays, the fast-ctv Prebid Server module, an origin rule that admits only the stage's MediaTailor requests, a bidder simulator with demo creatives, and census-gated scaling with warm-up | `ops` | `…-DemandEdge`, `…-DemandBuild`; with `make deploy-demand` the guidance (`<prefix>-pbs`), its own CDK bootstrap and `…-Demand` | Media: the kit encodes the demo creatives in demo mode | [demand.md](demand.md), [ADR-0020](adr/0020-demand-through-the-prebid-server-guidance.md) |
| `pds` | The Pod Decision Service: per break, what Prebid Server may ask third parties (bidders, regulations, device fields), and which ads make up the pod. Decision records go to the lake | `ops`, `demand` | With `make deploy-demand`, `…-Pds` in the guidance's VPC | Data: the table `<prefix>-pds`, a private config bucket and their names in SSM | [pds.md](pds.md) |
| `release` | The Release Controller for monetized playback configurations: sibling configurations, MediaTailor Functions, a smoke gate, gradual promotion and unattended rollback | `ops`, `demand`, `pds` | — so far | Media: each channel in `modules.release.channels` admits its sibling configurations (`<channel>-p-?`) | [channel-assembly.md](channel-assembly.md#iac-channels) |
| `crawl` | Show-level content metadata in ad requests: a catalog context object per title (title, series, IAB Content Taxonomy 3.1 genre and category, rating, language, producer), read by each break's UPID | `pds`, `release` | `FastChannels-<prefix>-Crawl` (the context publisher) | — | [content-enrichment.md](content-enrichment.md), [ADR-0031](adr/0031-show-level-content-metadata.md) |

**What is in place and what follows.**

| Module | In place | Follows |
|---|---|---|
| `ops` | Everything listed above. Runbooks run their dry-run path only: each records what it would do in the ledger | Acting unattended (L2 autonomy); the sibling configurations' detectors, with `release` |
| `models` | Registry, profiles, caps, eval records and the synth gate | The recommender's tasks and their eval suite |
| `demand` | Guidance, overlays, configuration, module, origin gate, image build, deploy and destroy, simulator, demo creatives, scaling and warm-up | The deploy, smoke and destroy leg in the end-to-end workflow; Prebid Server Java 4.4.0; a MediaTailor-driven smoke test of filled pods |
| `pds` | The rules and the v1 contract; the service with `POST /policy` and its decision records | The own book, reservations and delivery states; `/select`; fallback alarms |
| `release` | The desired-state model and its validators (`fast_channels.core.monetization`); the static channel-policy pattern | The apply path, the smoke gate, promotion, rollback and the origin-secret rotation |
| `crawl` | The catalog fields, the context object, the genre map and the publisher | The cached PRE_ADS lookup, the OpenRTB `app.content` patch and the uplift holdout |

A module whose code has not been merged yet changes nothing when it is enabled: its settings are validated, and
nothing is deployed.

## Turning modules on

Set `modules.<name>.enabled` and the module's other fields in `cdk.json`, or put them in a
[stage file](operations.md#stages). The [configuration reference](operations.md#configuration-reference) documents
every field. Synth refuses a module whose prerequisites are off ("`pds` needs `ops` and `demand`: enable them or
turn `pds` off"), and checks the module's own rules too:
- with `models`, every task's model must be allowed and qualified by a passing eval record;
- with `release`, its channels must be IaC channels;
- with `ops`, a `compliance` ledger lock needs `removal: retain`.

```bash
make synth STAGE=fastm                      # cdk-nag included; fails on any configuration problem
make deploy STAGE=fastm                     # the repository's stacks
make deploy-demand STAGE=fastm              # with demand: the guidance, Demand and Pds (about 30 minutes)
make seed STAGE=fastm SEED_ARGS="--wait --yes"
```

The repository ships two stages with every module on: `fastm` (development) and `fastci` (the end-to-end tests). Both
use the shared as-run router ([below](#account-level-limits-and-teardown-residue)).

Points to know before turning a module on:
- **`release` widens a channel's policy** to admit its sibling configurations. Changing the policy of a running
  channel would break its sessions, so the deployment refuses while the channel runs. Stop the channel, deploy, start
  it again.
- **`models` moves the scheduler agent onto its registry entry and profile.** Its role may then invoke only that
  profile, so changing the agent's model is a `cdk.json` change, a new eval record (`make eval TASK=scheduler.plan
  MODEL=<id>`) and a deployment.
- **`demand` in demo mode** re-provisions each ladder kit once, with ten 30 s demo creatives. It never writes to the
  ingest bucket.
- **`pds` and `demand` deploy in two steps.** `make deploy` creates the repository's stacks. The guidance and the
  stacks that live in its VPC (Demand, Pds) need `make deploy-demand`, because they need the guidance's network.

## Deploy and destroy order

`make deploy` deploys, by dependency:

```
Data → Media → Ops                          (ops)
     → Media → Crawl                        (crawl)
Models → Operator                           (models; Operator also after Data and Media)
DemandEdge (us-east-1), DemandBuild         (demand; no dependency)
```

`make deploy-demand` then runs, in order:
1. DemandEdge and DemandBuild again;
2. the guidance's own CDK bootstrap and its image;
3. the guidance;
4. the SSM contract `/<prefix>/demand/*`;
5. Demand, then Pds;
6. one run of each census gate;
7. the smoke test.

See [demand.md](demand.md#deploy-and-destroy) for the details.

Destroy in reverse:

```bash
make destroy-demand STAGE=fastm   # Pds, Demand, the guidance with what it retains, its bootstrap, DemandEdge/-Build
make destroy STAGE=fastm          # the repository's stacks
uv run python scripts/ledger_verify.py purge --prefix fastm --apply   # with ops: the Object-Locked ledger archive
```

`make destroy-demand` records what the guidance's templates retain before deleting anything, so an interrupted run
resumes where it stopped. Its `inventory` step exits non-zero while anything tagged `fast:module=demand` or `pds`
for the prefix is left. [Operations](operations.md#destroy) describes what `make destroy` removes and what it keeps.

## How each module fails safe

No module puts a dependency on the viewer's path that can stop playback. A module that fails costs ad revenue or
operations signal, never air time.

| Module | Failure | What happens |
|---|---|---|
| `demand` | The fast-ctv module is disabled or absent, or `/policy` is unreachable, slow or failing | Prebid Server's stored request names only the own book, so no third-party bidder is called (the no-hook contract, tested in every failure mode) |
| `demand` | Prebid Server is down or answers late | MediaTailor gets no pod, and the Channel Assembly slate plays the break |
| `demand` | A request without the stage's origin secret | The origin's WAF rule group blocks it at the edge (403) |
| `demand` | A break length off the 30.03 s grid | The stored request does not exist: Prebid Server answers 400 and the slate plays |
| `demand` | A new Prebid Server task is still cold | The warm-up waits until the task is a healthy target, then sends it probe auctions, which only the own book and house ads answer. Cold tasks time out on more `/policy` calls (2.6 % during a measured warm-up), so real traffic should start after it: open a daypart at least 10 minutes before its first break |
| `pds` | `/policy` misses its 80 ms timeout | The module serves the break from the own book only (`POLICY_UNAVAILABLE`). Measured at 200 auctions/s with 50 ms: 0.13 % of auctions fell back, p99 32 ms. A channel with breaks minutes apart pays the load balancer's Lambda invoke cold: 25-50 ms at the module, so 50 ms timed out one break in eight |
| `pds` | The function has never read its snapshot | It allows no third-party bidder and passes no viewer address |
| `pds` | `/select` is missing or failing | The module renders a house-only pod (`SELECT_FALLBACK`) |
| `crawl` | The publisher cannot use a catalog item | It logs the error, counts it in `ContextsInvalid` and leaves the title's object as it was. A trigger that still fails after its retries goes to a dead-letter queue, whose alarm `<prefix>-crawl-publisher-dead-letters` notifies the alert topic |
| `crawl` | With the PRE_ADS lookup (follows): the context object of a title cannot be read | The break is requested without the metadata |
| `release` | A deployment would change the policy of a running channel | The deployment fails before anything changes, and the channel keeps airing |
| `models` | The SSM entry is missing, invalid or names another model or profile | The agent runs on its entry as deployed and logs a warning |
| `models` | A task's day reaches its `daily_cap_usd` | Runs and model calls are refused with `MODEL_CAP_REACHED`, and the alert topic is told once that day. The cap is a breaker against a runaway, about ten times expected use |
| `models` | The spend counter is unreachable | Calls go ahead and the agent logs a warning |
| `ops` | A detector, the poller or the probe fails | Operations signal is lost, not air time. The module's alarms treat missing data as not breaching, so a quiet stage does not page |
| `ops` | A runbook's trigger fires | The runbook records its decision in the ledger and acts on nothing: acting unattended is not enabled yet |
| `ops` | A ledger record is modified, dropped, inserted or reordered | `scripts/ledger_verify.py verify` fails, and the table's alarms catch a changed or rejected entry |
| `ops` | Channel Assembly logs no `AdBreakStart` for a break that started within about a minute of `StopChannel` | The sweep marks that break `UNAIRED` although it aired ([channel-assembly.md](channel-assembly.md#the-schedule-of-record-and-the-epg)) |

AppConfig kill switches (the `flags` profile of `<prefix>-fast`) turn off each module's consumers at runtime without
a deployment ([observability.md](observability.md#runbooks-flags-and-the-probe)).

## What the modules cost

These are us-east-1 list prices, rounded. Lines marked *measured* come from runs on development stages in October
2026. Your bill is authoritative.

| Module | While channels are stopped | While channels air |
|---|---|---|
| `ops` | Its alarms at $0.10 each a month (12 on `fastm`, 4 of them per IaC playback configuration); cents of scheduled functions and state machines; lake and ledger storage | The probe canary ($0.0012 a run, every 5 minutes, about $0.35 a day); the alerts poller every minute; Firehose, S3 and Athena by the GB (the workgroup cancels a query past 10 GiB scanned) |
| `models` | Nothing: an on-demand table, standard SSM parameters, and inference profiles, which cost nothing of their own | Bedrock tokens as before (about $0.003–0.006 per schedule with Claude Haiku 5.5), plus a few DynamoDB writes per run |
| `demand`, before `make deploy-demand` | Two Secrets Manager secrets ($0.40 each a month), the WAF rule group, the image repository's storage | — |
| `demand`, guidance deployed | About $120–150 a month: two NAT gateways, the load balancers, the web ACL and the Valkey cache minimum. The `guidance_stack` simulator (the default) adds its own VPC with two more NAT gateways and a load balancer, about $90 a month more at list prices; the `lambda` simulator adds an internal load balancer instead (about $16 a month) | Prebid Server tasks at $0.0987 per task-hour (2 vCPU, 4 GB, x86 Fargate). The tier minimum (`tasks.min`) is 2 tasks, scaled to 0 fifteen minutes after the last channel stops |
| `pds` | Its internal load balancer (about $16 a month); the table and buckets on demand | Provisioned concurrency on the `/policy`, `/select` and `/ownbook/openrtb` aliases (2 × 1,769 MB each on arm64 by default, about $0.04 an hour each), set and removed by the census gate |
| `release` | Nothing: so far it only widens channel policies | — |
| `crawl` | One alarm ($0.10 a month); the publisher's runs, S3 objects and CloudFront requests cost well under a cent at demo scale (*measured*) | The same |

*Measured* runs:
- a demand deploy, smoke test and destroy cycle cost about $0.45. The destroy took 85 minutes, 59 of them for the
  guidance's ElastiCache user group;
- 17 minutes of a monetized stage airing one channel cost about $0.25 more than the stage's idle cost: two Prebid
  Server tasks and their warm-up, the PDS's provisioned concurrency, and two load tests at 200 auctions/s.

Destroy throwaway stages with the guidance deployed right after use: they cost by the hour even with every channel
stopped.

## Account-level limits and teardown residue

The first rows apply per account and Region, whatever the number of stages; the last ones are what a stage's
teardown leaves behind.

| Limit | Cause | What to do |
|---|---|---|
| **At most 5 `per_stage` deployments** | Each Media stack with `observability.as_run: per_stage` (the v3.0.0 default) subscribes its as-run reconciler to the account-shared log group `MediaTailor/Channel/AsRunLog`. CloudWatch Logs allows 5 subscription filters per log group (`L-87E7D306`, not adjustable). A sixth deployment fails to create its filter, and its Media stack rolls back | Use `as_run: shared`. Deploy the account's as-run router once (`make as-run-router STAGE=<stage>`, the termination-protected stack `FastChannels-AsRun`, [ADR-0032](adr/0032-shared-as-run-router.md)): it takes one filter and puts every event on the default event bus, and `shared` deployments take none. Count the filters before deploying a `per_stage` stage: `aws logs describe-subscription-filters --log-group-name MediaTailor/Channel/AsRunLog` |
| **One Prebid Server guidance per account without overlay O1's Glue fix** | Guidance v1.4.0 names its MetricsEtl Glue database `prebid-server-<region>-metricsetl-database`, with no stack prefix, so a second guidance stack fails with `Database already exists` | Overlay O1 prefixes it (`<prefix>-prebid-server-<region>-metricsetl-database`), so stages can share an account. A guidance deployed before the fix keeps the unprefixed name until its next `make deploy-demand`, in which CloudFormation replaces the database. Until then no second guidance fits beside it |
| **VPCs and Elastic IP addresses** | The guidance has its own VPC with two NAT gateways (2 addresses); the `guidance_stack` simulator adds a second VPC and two more. The default quotas are 5 VPCs and 5 Elastic IPs per Region | One demand stage uses 1 VPC and 2 addresses with `simulator: lambda`, or 2 and 4 with `guidance_stack`. Request increases before running several |
| **Prefixes without hyphens** | Each stage's resource names and IAM grants are scoped by `<prefix>-*`, so a prefix such as `fast-x` would fall inside the grants of a stage `fast` | Synth refuses a prefix with a hyphen; name stages like `fastci`, `fastcih` or `fastciads` |
| **Demand prefix of at most 10 characters** | The guidance's dedicated CDK bootstrap uses the prefix as its qualifier | Synth refuses a longer prefix with demand on |
| **KMS keys pending deletion** | `make destroy-demand` schedules the guidance's six KMS keys for deletion with the 7-day minimum | They bill nothing while pending; they are the only residue the inventory allows |
| **CDK LogRetention log groups after `make destroy`** | Deleting the Operator stack runs CDK's log-retention provider once more, which recreates two log groups no stack owns, one of them without retention: `FastChannels-<prefix>-Operator-LogRetention…ResourceLogs…` and `/aws/lambda/FastChannels-<prefix>-Oper-LogRetention…`. This is v3.0.0 behaviour, with or without modules | Delete both by hand after the destroy: find them with `aws logs describe-log-groups --log-group-name-pattern LogRetention`, then `aws logs delete-log-group --log-group-name <name>` |
| **The probe canary's function logs** | CloudWatch Synthetics creates `/aws/lambda/cwsyn-<prefix>-probe-*` at the canary's first run and deletes only the function with the canary | Delete the log group by hand after `make destroy` |
| **The ledger archive** | With `ledger_lock` `governance` or `compliance` the archive is retained and its records are under Object Lock | After `make destroy`, `scripts/ledger_verify.py purge --prefix <prefix> --apply` deletes the versions whose retention has passed, then the bucket; in governance mode `--apply --bypass-governance` deletes it at once. Development stages use `ledger_retention_days: 1` |

The `FastChannels-AccountBaseline` (Transaction Search, `make account-baseline`) and `FastChannels-AsRun`
(`make as-run-router`) stacks are account-level and termination protected. Each has its own make target, and
neither `make deploy` nor `make destroy` selects them, so their protection never stops a teardown part-way. Delete
the router only once no deployment in the account and Region is `shared`, and the baseline only once none uses
`observability.transaction_search` `enable` or `existing`.
