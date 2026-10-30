# Optional modules: ad monetization and operations

FAST Channels v3.0.0 airs channels with preconditioned house ads. The optional modules of v4.0.0 add:

- programmatic demand through Prebid Server, optionally behind your ad server;
- a decision service for every ad break, with an own book of direct lines and house ads;
- show-level metadata in ad requests;
- a release process for monetized playback configurations, with ad prefetch;
- a per-task model registry;
- the operations they need: a lake, a hash-chained ledger, detectors, runbooks and a probe.

Every module is off by default, behind `modules.<name>.enabled` in [`cdk.json`](../cdk.json). With every module off
a deployment is v3.0.0's, which snapshot tests of every shipped template check (`infra/tests/test_snapshots.py`).

- [The modules](#the-modules)
- [What v4.0.0 does not do](#what-v400-does-not-do)
- [Turning modules on](#turning-modules-on)
- [Deploy and destroy order](#deploy-and-destroy-order)
- [Capacity](#capacity)
- [How each module fails safe](#how-each-module-fails-safe)
- [What the modules cost](#what-the-modules-cost)
- [Account-level limits and teardown residue](#account-level-limits-and-teardown-residue)

## The modules

| Module | What it does | Needs | Adds | Changes | Docs and decisions |
|---|---|---|---|---|---|
| `ops` | A per-stage lake for Athena over the CDN, MediaTailor and decision logs. A hash-chained, Object-Locked ledger of unattended actions and money events. Account quota and hook alarms, a zero-ad-sessions detector and a Channel Assembly alerts poller. Break ids in the EPG and `aired_at` from the as-run log. Eight runbooks, AppConfig kill switches, and a probe canary that plays every running channel | — | `FastChannels-<prefix>-Ops` | Data: the lake bucket, the ledger table and its archive. Media: `Census Changed` events, `AdBreakStart` reconciliation and the break sweep, break ids in the EPG feed | [Lake](observability.md#lake), [ledger](observability.md#ledger), [runbooks, flags and the probe](observability.md#runbooks-flags-and-the-probe), [break reconciliation](channel-assembly.md#the-schedule-of-record-and-the-epg); [ADR-0022](adr/0022-delivery-states-billing-and-the-hash-chained-ledger.md), [0032](adr/0032-shared-as-run-router.md) |
| `models` | A per-task model registry over Bedrock Converse. Each LLM task gets a validated SSM entry, its own application inference profile (its own metrics and cost line), a daily spend breaker and an eval gate | — | `FastChannels-<prefix>-Models` | Operator: the scheduler agent reads its `scheduler.plan` entry and may invoke only its profile | [Model registry](agent.md#model-registry); [ADR-0029](adr/0029-per-task-model-registry.md) |
| `demand` | An OpenRTB auction behind the ad breaks. It runs the AWS Prebid Server guidance at a pinned tag, with the repository's overlays, on Prebid Server Java 3.43.0 or 4.4.0. The fast-ctv Prebid Server module asks the Pod Decision Service and renders one VAST pod. An origin rule admits only the stage's MediaTailor requests. A bidder simulator with demo creatives, and census-gated scaling with warm-up. Optionally, an ad server in front: each break's auction result is cached in Prebid Cache for the ad server's Prebid line items (`ad_server`) | `ops` | `…-DemandEdge`, `…-DemandBuild`; with `make deploy-demand`: the guidance (`<prefix>-pbs`), its own CDK bootstrap, and `…-Demand` | Media: the kit encodes the demo creatives in demo mode | [demand.md](demand.md); [ADR-0020](adr/0020-demand-through-the-prebid-server-guidance.md), [0035](adr/0035-prebid-results-for-an-ad-servers-vast.md) |
| `pds` | The Pod Decision Service. Per break, it decides what Prebid Server may ask third parties (bidders, regulations, device fields) and which ads make up the pod. It also runs the own book (house ads and direct lines with budgets), reservations, delivery from our impression tracker, and a decision record per call into the lake | `ops`, `demand` | With `make deploy-demand`: a sidecar in each Prebid Server task, and `…-Pds` beside the guidance | Data: the table `<prefix>-pds`, a private config bucket and their names in SSM | [pds.md](pds.md); [ADR-0036](adr/0036-pds-sidecar-in-the-prebid-server-task.md), [0022](adr/0022-delivery-states-billing-and-the-hash-chained-ledger.md) |
| `release` | The Release Controller for monetized playback configurations. Each monetized channel gets sibling configurations with MediaTailor Functions, deployed one release at a time: a smoke gate, then gradual promotion with unattended rollback. It also handles the origin-secret rotation, ad prefetch with one SINGLE schedule per upcoming break, Function caches declared by scope, drift and log-reconciliation checks, and an ownership guard on every other role | `ops`, `demand`, `pds` | With `make deploy-demand`: `…-Release` | Media: each monetized channel admits its sibling configurations (`<channel>-p-?`), and the stopped lab channel `<prefix>-lab`. Every stack: the `ReleaseGuard` policy | [release-controller.md](release-controller.md); [ADR-0019](adr/0019-functions-release-controller-siblings.md), [0021](adr/0021-threshold-121-slate-and-constant-session-controls.md), [0023](adr/0023-machine-principals.md), [0033](adr/0033-prefetch-single-schedules.md), [0034](adr/0034-every-function-request-cached-by-scope.md) |
| `crawl` | Show-level content metadata in ad requests. It keeps a catalog context object per title (title, series, IAB Content Taxonomy 3.1 genre and category, rating, language, producer). A cached PRE_ADS lookup by the break's UPID reads it, and OpenRTB 2.6 `app.content` carries it, with an uplift holdout | `pds`, `release` | `FastChannels-<prefix>-Crawl` (the context publisher) | Release: the PRE_ADS lookup in the bundle. Pds: the `app.content` patch | [content-enrichment.md](content-enrichment.md); [ADR-0031](adr/0031-show-level-content-metadata.md) |

## What v4.0.0 does not do

- **Runbooks only record.** Each runbook writes the action it would take to the ledger and acts on nothing.
  Unattended action (L2 autonomy) is not enabled. The Release Controller's promotion and rollback are the exception
  ([ADR-0023](adr/0023-machine-principals.md)).
- **No live third-party demand.** `modules.demand.mode` is `own_book` (direct lines and house ads) or `demo` (the
  bidder simulator, labelled simulated everywhere). Connecting real SSPs and bidders is configuration that ships
  untested.
- **`gam` and `freewheel` are untested live.** The ad-server modes `gam` and `freewheel` are configuration and setup
  instructions. Only `mock`, a GAM-like ad server that the stage deploys, is tested end to end.
- **The models registry serves one task.** It carries the scheduler agent's `scheduler.plan`. The recommender's
  tasks and eval suite are defined but not built.

## Turning modules on

Set `modules.<name>.enabled` and the module's other fields in `cdk.json`, or put them in a
[stage file](operations.md#stages). The [configuration reference](operations.md#configuration-reference) documents
every field. Synth refuses a module whose prerequisites are off ("`pds` needs `ops` and `demand`: enable them or
turn `pds` off"). It checks each module's own rules too:

- with `models`, every task's model must be allowed and qualified by a passing eval record;
- with `release`, its channels must be IaC channels, and no IaC channel may be called `<prefix>-lab` while
  `lab_channel` is on;
- with `demand`, the prefix is at most 10 characters, and `ad_server` needs `release`;
- with `ops`, a `compliance` ledger lock needs `removal: retain`.

```bash
make synth STAGE=fastm                      # cdk-nag included; fails on any configuration problem
make deploy STAGE=fastm                     # the repository's stacks
make deploy-demand STAGE=fastm              # with demand: the guidance, Demand, Pds and Release (about 30 minutes)
make seed STAGE=fastm SEED_ARGS="--wait --yes"
```

The repository ships three stages with every module on:

- `fastm`, for development;
- `fastci`, for the end-to-end tests;
- `fastciads`, which is `fastci` with the `mock` ad server.

All three use the shared as-run router ([below](#account-level-limits-and-teardown-residue)).

Points to know before turning a module on:

- **`release` widens a channel's policy** to admit its sibling configurations. Changing the policy of a running
  channel would break its sessions, so the deployment refuses while the channel runs. Stop the channel, deploy, start
  it again.
- **`release` owns the monetized configurations.** With the module on, every other role of the stage is denied the
  writes the controller makes (the managed policy `ReleaseGuard`). The agent tools, scripts and the console can no
  longer change the siblings.
- **`models` moves the scheduler agent onto its registry entry and profile.** Its role may then invoke only that
  profile, so changing the agent's model takes three steps:
  1. a `cdk.json` change;
  2. a new eval record (`make eval TASK=scheduler.plan MODEL=<id>`);
  3. a deployment.
- **`demand` in demo mode** re-provisions each ladder kit once, with ten 30 s demo creatives. It never writes to the
  ingest bucket.
- **`demand`, `pds` and `release` deploy in two steps.** `make deploy` creates the repository's stacks. The guidance
  and the stacks that need its network or its CloudFront domain (Demand, Pds, Release) need `make deploy-demand`.
- **Prefixes and channel names.** A prefix has no hyphen. No IaC channel may be named another channel's name plus
  `-p-…`, or `<prefix>-agent`: those names belong to the release module's siblings
  ([below](#account-level-limits-and-teardown-residue)).

## Deploy and destroy order

`make deploy` deploys, by dependency:

```
Data → Media → Ops                          (ops)
     → Media → Crawl                        (crawl)
Models → Operator                           (models; Operator also after Data and Media)
DemandEdge (us-east-1), DemandBuild         (demand; no dependency)
```

`make deploy-demand` then runs these steps in order:

1. DemandEdge and DemandBuild again.
2. The guidance's own CDK bootstrap, and the images: Prebid Server with the fast-ctv module, and the PDS sidecar.
3. The guidance. With `pds` on it goes through a thin wrapper of the guidance's own app, which adds the sidecar
   container to the Prebid Server task. With `ad_server` on, the wrapper also gives the Prebid Cache function its
   provisioned concurrency.
4. The SSM contract `/<prefix>/demand/*`.
5. The rendered Prebid Server configuration: what differs is uploaded and read back.
6. Demand, then Pds, then Release.
7. One run of each census gate.
8. The smoke test.

See [demand.md](demand.md#deploy-and-destroy) for the details.

Destroy in reverse:

```bash
make destroy-demand STAGE=fastm   # Release, Pds, Demand, the guidance with what it retains, its bootstrap, DemandEdge/-Build
make destroy STAGE=fastm          # the repository's stacks
uv run python scripts/ledger_verify.py purge --prefix fastm --apply   # with ops: the Object-Locked ledger archive
```

Deleting the Release stack runs its reaper first. The reaper deletes:

- the siblings, with their prefetch schedules and log deliveries;
- the Functions;
- the playback plans.

Each deletion is checked against the resource's ownership tag. `make destroy-demand` records what the guidance's
templates retain before deleting anything, so an interrupted run resumes where it stopped. Its `inventory` step exits
non-zero while anything tagged `fast:module=demand` or `pds` for the prefix is left.
[Operations](operations.md#destroy) describes what `make destroy` removes and what it keeps.

## Capacity

These figures were measured on development stages in October 2026.

- **Viewers.** The modules do not change v3.0.0's delivery path. MediaTailor's Ad Insertion requests quota still
  binds, at about 37,000 concurrent viewers per account at 70 % of the default quotas
  ([delivery-and-multi-cdn.md](delivery-and-multi-cdn.md#capacity)). The ops module alarms at 80 % of that quota and
  of Channel Assembly's.
- **A monetized break.** Two Prebid Server tasks (the tier minimum, `tasks.min`), each with its PDS sidecar,
  answered a 400-session real-time break herd with no fallback beyond one `/select` at its timeout:

  | Call | p99 |
  |---|---|
  | `/policy` at the fast-ctv module | 18 ms |
  | `/select` | 134 ms |

  The `/select` time is bound by the reservation's DynamoDB transaction.
  - With prefetch, the same herd spreads over the retrieval window: break-3 `/select` p95 28 ms.
  - Past about 400 real-time sessions per playback configuration per break, the reservation's four budget shards
    start to contend. Revisit `modules.pds` reservations then ([pds.md](pds.md#reservations-and-delivery)).
- **More traffic.** Raise `modules.demand.tasks.min` and `max`, and add dayparts so the tasks are warm before the
  break. A new task gets its warm-up and ALB slow start before it takes its share
  ([demand.md](demand.md#scaling-the-census-gate-dayparts-and-warm-up)).
- **The ad-server mode.** A real-time break needs about 0.06–0.08 ready Prebid Cache instances per session. Set
  `pod_cache_concurrency` from the audience of the largest break ([demand.md](demand.md#the-cache-function)).

## How each module fails safe

No module puts a dependency on the viewer's path that can stop playback. A module that fails costs ad revenue or
operations signal, never air time.

| Module | Failure | What happens |
|---|---|---|
| `demand` | The fast-ctv module is disabled or absent, or `/policy` is unreachable, slow or failing | Prebid Server's stored request names only the own book, so no third-party bidder is called (the no-hook contract, tested in every failure mode) |
| `demand` | Prebid Server is down or answers late | MediaTailor gets no pod, and the Channel Assembly slate plays the break |
| `demand` | A request without the stage's origin secret | The origin's WAF rule group blocks it at the edge (403) |
| `demand` | A break length off the 30.03 s grid | The stored request does not exist: Prebid Server answers 400 and the slate plays |
| `demand` | A new Prebid Server task is still cold | The warm-up waits until the task is a healthy target, then sends it probe auctions, which only the own book and house ads answer. ALB slow start (180 s) ramps live traffic onto a task that joins a serving fleet; this does not apply to a scale from zero. Cold tasks time out on more `/policy` calls (2.6 % during a measured warm-up), so real traffic should start after it: open a daypart at least 10 minutes before its first break |
| `demand` | With an ad server: the pod's cache write misses the exitpoint's time | The break answers no bid, and the ad server serves its house ad |
| `pds` | `/policy` misses its 50 ms timeout | The module serves the break from the own book only (`POLICY_UNAVAILABLE`). If that pod would be empty, it renders the kit's house ads from its configuration (`HOUSE_CONFIG`) |
| `pds` | The sidecar has never read its snapshot | `/policy` and `/select` answer 503 at once. The module serves the break SAFE and, with the own book silent, renders the kit's house ads (`HOUSE_CONFIG`) |
| `pds` | The sidecar is overloaded | A request that waited for a worker past its caller's timeout is answered 503 at once (`Shed`). The module falls back for that break, and the queue drains |
| `pds` | The sidecar exits | ECS restarts it in place. Meanwhile the module's calls are refused, and it falls back as above |
| `pds` | `/select` is missing or failing | The module renders a house-only pod (`SELECT_FALLBACK`), or the kit's house ads without one (`HOUSE_CONFIG`). A paid line never airs without a reservation |
| `release` | A deployment would change the policy of a running channel | The deployment fails before anything changes, and the channel keeps airing |
| `release` | A release fails its smoke test or soak, its pointer deployment rolls back, or the candidate alarm fires | Runbook R1 remaps the sibling to its last good Function tree, and the playback plan names the stable sibling again |
| `release` | No monetized channel runs and there is no lab channel to start | The release stays applied and is not promoted (`release.hold` in the ledger) |
| `release` | A sibling has a RECURRING prefetch schedule | It gets no SINGLE schedule, and `<prefix>-release-prefetch` fires. Its breaks are requested in real time, like every break the prefetcher leaves alone ([release-controller.md](release-controller.md#prefetch)) |
| `release` | An origin-secret rotation fails part-way | Both of the gate's values stay admitted, so the siblings keep sending one it accepts. `<prefix>-release-rotation-failed` notifies the alert topic |
| `crawl` | The publisher cannot use a catalog item | It logs the error, counts it in `ContextsInvalid` and leaves the title's object as it was. A trigger that still fails after its retries goes to a dead-letter queue, whose alarm `<prefix>-crawl-publisher-dead-letters` notifies the alert topic |
| `crawl` | The PRE_ADS lookup cannot read a title's context object | The break is requested without the metadata: the lookup fails open |
| `models` | The SSM entry is missing, invalid or names another model or profile | The agent runs on its entry as deployed and logs a warning |
| `models` | A task's day reaches its `daily_cap_usd` | Runs and model calls are refused with `MODEL_CAP_REACHED`, and the alert topic is told once that day. The cap is a breaker against a runaway, about ten times expected use |
| `models` | The spend counter is unreachable | Calls go ahead and the agent logs a warning |
| `ops` | A detector, the poller or the probe fails | Operations signal is lost, not air time. The module's alarms treat missing data as not breaching, so a quiet stage does not page |
| `ops` | A runbook's trigger fires | The runbook records its decision in the ledger and acts on nothing ([above](#what-v400-does-not-do)) |
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
| `demand`, before `make deploy-demand` | Two Secrets Manager secrets ($0.40 each a month), the WAF rule group, the image repositories' storage | — |
| `demand`, guidance deployed | About $120–150 a month: two NAT gateways, the load balancers, the web ACL and the Valkey cache minimum. The `guidance_stack` simulator adds its own VPC with two more NAT gateways and a load balancer, about $90 a month more. The `lambda` simulator (on the shipped stages) adds an internal load balancer instead, about $16 a month. With an ad server, the Prebid Cache function's provisioned concurrency (8 × 1,769 MB) adds about $0.17 an hour while the stage exists | Prebid Server tasks on x86 Fargate at 2 vCPU: $0.0987 per task-hour with 4 GB, or about $0.108 with the 6 GiB that the PDS sidecar's task needs. The tier minimum (`tasks.min`) is 2 tasks, scaled to 0 fifteen minutes after the last channel stops |
| `pds` | The table and buckets on demand; the snapshot publisher, delivery and sweeper functions; its alarms | Nothing beyond the Prebid Server tasks: the service runs in them (ADR-0036) |
| `release` | Its KMS key ($1 a month); its alarms and composite alarm; the hourly drift check and log reconciliation; AppConfig; cents of state machines | The prefetcher's runs every 5 minutes while a monetized channel runs; the smoke test's probe sessions for each release |
| `crawl` | One alarm ($0.10 a month). The publisher's runs, S3 objects and CloudFront requests cost well under a cent at demo scale (*measured*) | The same, plus the cached PRE_ADS lookup's requests |

*Measured* runs:

- **A demand deploy, smoke test and destroy cycle:** about $0.45. The destroy took 85 minutes, 59 of them for the
  guidance's ElastiCache user group.
- **One channel airing on a monetized stage, 17 minutes:** about $0.25 more than the stage's idle cost. That covers
  two Prebid Server tasks and their warm-up, the PDS, and two load tests at 200 auctions/s.
- **The ad-server end-to-end leg:** about $4 for a run with the guidance up for four hours.

Destroy throwaway stages with the guidance deployed right after use: they cost by the hour even with every channel
stopped.

## Account-level limits and teardown residue

The first rows apply per account and Region, whatever the number of stages. The last ones are what a stage's teardown
leaves behind.

| Limit | Cause | What to do |
|---|---|---|
| **At most 5 `per_stage` deployments** | Each Media stack with `observability.as_run: per_stage` (the v3.0.0 default) subscribes its as-run reconciler to the account-shared log group `MediaTailor/Channel/AsRunLog`. CloudWatch Logs allows 5 subscription filters per log group (`L-87E7D306`, not adjustable). A sixth deployment fails to create its filter, and its Media stack rolls back | Use `as_run: shared`. Deploy the account's as-run router once (`make as-run-router STAGE=<stage>`, the termination-protected stack `FastChannels-AsRun`, [ADR-0032](adr/0032-shared-as-run-router.md)): it takes one filter and puts every event on the default event bus, and `shared` deployments take none. Count the filters before deploying a `per_stage` stage: `aws logs describe-subscription-filters --log-group-name MediaTailor/Channel/AsRunLog` |
| **IAM roles: about 140 per full stage** | A stage with every module on and the guidance deployed holds about 140 IAM roles: function roles, state machines, custom resources, the guidance's own. The default quota is 1,000 roles per account, shared with everything else in it. A deployment that hits it fails to create a role and rolls back | Count before deploying several full stages: `aws iam list-roles --query 'length(Roles)'`. Request a quota increase, or destroy idle stages |
| **One Prebid Server guidance per account without overlay O1's Glue fix** | Guidance v1.4.0 names its MetricsEtl Glue database `prebid-server-<region>-metricsetl-database`, with no stack prefix, so a second guidance stack fails with `Database already exists` | Overlay O1 prefixes it (`<prefix>-prebid-server-<region>-metricsetl-database`), so stages can share an account. A guidance deployed before the fix keeps the unprefixed name until its next `make deploy-demand`, in which CloudFormation replaces the database. Until then no second guidance fits beside it |
| **VPCs and Elastic IP addresses** | The guidance has its own VPC with two NAT gateways (2 addresses). The `guidance_stack` simulator adds a second VPC and two more. The default quotas are 5 VPCs and 5 Elastic IPs per Region | One demand stage uses 1 VPC and 2 addresses with `simulator: lambda`, or 2 and 4 with `guidance_stack`. Request increases before running several |
| **Prefixes without hyphens** | Each stage's resource names and IAM grants are scoped by `<prefix>-*`, so a prefix such as `fast-x` would fall inside the grants of a stage `fast` | Synth refuses a prefix with a hyphen. Name stages like `fastci`, `fastcih` or `fastciads` |
| **Channel names outside the siblings' names** | The release module names a monetized channel's playback configurations `<channel>-p-a`, `-p-b` and `-p-z`, and its grants cover `<channel>-p-*` | Synth refuses an IaC channel named another channel's (or `<prefix>-lab`'s) name plus `-p-…`, with the module on or off, and one named `<prefix>-agent`, whose siblings would be agent-owned names |
| **Demand prefix of at most 10 characters** | The guidance's dedicated CDK bootstrap uses the prefix as its qualifier | Synth refuses a longer prefix with demand on |
| **KMS keys pending deletion** | `make destroy-demand` schedules the guidance's six KMS keys for deletion with the 7-day minimum, and the release module's log key with CDK's default 30-day window | They bill nothing while pending. They are the only residue the demand inventory allows |
| **CDK LogRetention log groups after `make destroy`** | Deleting the Operator stack runs CDK's log-retention provider once more. It recreates two log groups that no stack owns, one of them without retention: `FastChannels-<prefix>-Operator-LogRetention…ResourceLogs…` and `/aws/lambda/FastChannels-<prefix>-Oper-LogRetention…`. This is v3.0.0 behaviour, with or without modules | Delete both by hand after the destroy. Find them with `aws logs describe-log-groups --log-group-name-pattern LogRetention`, then run `aws logs delete-log-group --log-group-name <name>` |
| **The probe canary's function logs** | CloudWatch Synthetics creates `/aws/lambda/cwsyn-<prefix>-probe-*` at the canary's first run and deletes only the function with the canary | Delete the log group by hand after `make destroy` |
| **The ledger archive** | With `ledger_lock` `governance` or `compliance` the archive is retained and its records are under Object Lock | After `make destroy`, `scripts/ledger_verify.py purge --prefix <prefix> --apply` deletes the versions whose retention has passed, then the bucket. In governance mode `--apply --bypass-governance` deletes it at once. Development stages use `ledger_retention_days: 1` |

The `FastChannels-AccountBaseline` (Transaction Search, `make account-baseline`) and `FastChannels-AsRun`
(`make as-run-router`) stacks are account-level and termination protected. Each has its own make target, and
neither `make deploy` nor `make destroy` selects them, so their protection never stops a teardown part-way.

- Delete the router only once no deployment in the account and Region is `shared`.
- Delete the baseline only once no deployment uses `observability.transaction_search` `enable` or `existing`.
