# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Version 4.0.0 is a major release. It adds the optional monetization and operations modules
([docs/modules.md](docs/modules.md)): `ops`, `models`, `demand`, `pds`, `release` and `crawl`. With every module
off a deployment is v3.0.0's, which snapshot tests of every shipped template check. The BREAKING items apply
whatever the modules.

<!-- Releasing v4.0.0: move the `channels___create_channel` entry from [3.0.0] "Fixed since the first v3.0.0
tag" into this release's Fixed section, unless v3.0.0 is re-cut with it first. -->

### BREAKING

- **Deployment prefixes have no hyphen** (`prefix`: 2-16 lowercase letters and digits, starting with a letter). A
  deployment's resource names and IAM grants are scoped by `<prefix>-*`, so a prefix such as `fast-x` fell inside
  the grants of the deployment `fast`: `fast-*` matched `fast-x-*` resources. Synth now refuses such a prefix and says
  why. A v3.0.0 deployment whose prefix has a hyphen must be redeployed under a new prefix, because resource names
  derive from it. The shipped prefixes (`fast` and the stages) comply. `stages/*.json`, `scripts/stage_args.py`
  `--prefix` and the e2e workflow's prefix input apply the same rule.
- **IaC channel names cannot collide with the release module's siblings.** The release module names a monetized
  channel's playback configurations `<channel>-p-a`, `-p-b` and `-p-z`, and the controller's IAM grants and the
  release guard cover `<channel>-p-*`. An IaC channel called `fast-demo-p-a` beside `fast-demo` was accepted: its
  playback configuration would have been a sibling's name, inside the controller's grants. Synth now refuses a
  channel named another channel's name (or the lab channel `<prefix>-lab`'s) plus `-p-...`, whether or not the
  release module is on, and an IaC channel named `<prefix>-agent`, whose siblings would fall in the agent-owned
  `<prefix>-agent-*` names. The error says why. The shipped stages comply.
- **With `observability.transaction_search: enable`, run `make account-baseline` once per account and Region
  before `make deploy`.** `cdk destroy '**'` selected the termination-protected `FastChannels-AccountBaseline`
  stack, which CloudFormation refuses to delete, so `make destroy` failed after the Operator stack and left the
  stacks below it. The baseline now has its own target, `make account-baseline` (context `account-baseline=true`;
  refused unless `transaction_search` is `enable`), like the as-run router, and `make deploy` and `make destroy`
  never select it. With `transaction_search` off (the default) every template is unchanged.

### Added

- **Modules configuration**: a `modules` section in the `fast-channels` block with one validated section per optional
  monetization module (`ops`, `models`, `demand`, `pds`, `release`, `crawl`), every field spelled out in
  `cdk.json` and every module off. Modules that build on others refuse to be enabled alone, and unknown keys are
  refused. With every module off the deployment is exactly v3.0.0: snapshot tests now cover every template of the
  shipped configuration.
- **Stages**: `stages/<name>.json` holds a named deployment's overrides (a partial `fast-channels` block), and
  `STAGE=<name>` applies it to `make synth`, `diff`, `deploy`, `destroy`, `seed` and `trace`
  (`scripts/stage_args.py`). Three stages ship, each with every module on: `fastm`, `fastci` and `fastciads` (the
  ad-server leg).
- **Module documentation** ([docs/modules.md](docs/modules.md)):
  - what each module does, and what v4.0.0 leaves out;
  - how to turn a module on, and the deploy and destroy order;
  - the measured capacity of a monetized break;
  - how each module fails safe, and what it costs;
  - the limits that stages share in an account: at most five `per_stage` as-run subscriptions, about 140 IAM roles
    per full stage, one Prebid Server guidance per account without overlay O1's Glue fix, VPC and Elastic IP quotas,
    and the prefix and channel-name rules;
  - the teardown residue: CDK's log-retention log groups, the probe canary's function logs, KMS keys pending
    deletion, and a locked ledger archive.

  The operations page covers deploy order, stages, teardown and the modules' security controls. The architecture and
  README pages cover the module stacks, their outputs and constructs.
- **Correlation chains and `scripts/trace.py`** ([docs/observability.md](docs/observability.md#correlation-chains)):
  - the five chains (viewer, rail, recommender, content, change), and the keys that join each one;
  - `trace.py` also follows a decision id `<sid>.<evid>` (with its session), a release's change id and its release
    tag `rNNNN` (each to the other, through the Release Controller's state table);
  - it searches the modules' log groups: the PDS sidecar, Prebid Server's container log, the siblings' logs and the
    module stacks;
  - `--lake` adds an Athena leg over the decision records, the MediaTailor log copies and the archived ledger;
  - a name prefix that ends in the deployment prefix no longer picks up another stage's log groups (`fastci` and
    `fastciads`).
- **Ops module, lake and census events** (`modules.ops.enabled`): a per-stage lake (bucket in the Data stack;
  `FastChannels-<prefix>-Ops` with the Glue database `<prefix>_lake`, the Athena workgroup `<prefix>-lake` with a
  10 GiB scan limit, and the views `v_breaks` and `v_fill_by_pc`). The lake reads the CloudFront Parquet logs
  without `c-ip`, `cs-uri-query` and `x-forwarded-for`, and receives JSON copies of each IaC playback
  configuration's MediaTailor ad decision server logs (without `requestHeaders` and `rawAdsRequest`) and manifest
  logs (without `mediaTailorPath`, `input` and `http`). The channel census puts `Census Changed` on the bus when the
  number of running channels crosses zero. The AppConfig application `<prefix>-fast` and environment `<prefix>` are
  created for the `flags` and `release` profiles.
- **Ops module, hash-chained ledger**: producers append entries to `<prefix>-ledger` with a conditional put; a writer
  on the table's stream links each into its chain (sequence number and SHA-256 of the record before, one chain per
  `chain_id`) and Firehose archives the records in an S3 Object Lock bucket (`modules.ops.ledger_lock`,
  `ledger_retention_days`) that outlives its stack. `scripts/ledger_verify.py` verifies every chain against its head,
  writes synthetic records end to end, and purges a destroyed stage's archive.
- **Ops module, break ids and as-run breaks**: the EPG JSON feed's breaks carry their SCTE-35 `event_id` and `upid`;
  the as-run reconciler also reads `AdBreakStart` events and marks the scheduled break `AIRED` with its `aired_at`,
  and a sweep every 15 minutes marks the breaks that never aired `UNAIRED`. Without the module the feed and the
  reconciler are v3.0's.
- **Ops module, alarms and detectors**: account quota alarms at 80 % of the ad insertion (10,000/s) and Channel
  Assembly (400/s) request quotas, summed over the account with CloudWatch Metrics Insights; hook and manifest error
  alarms per IaC playback configuration; an hourly detector of sessions that saw three or more breaks without an ad
  request (Logs Insights, with the lake view `v_zero_ads_sessions` per day); and a poller that publishes
  `Channel Alert` events for the alerts of running channels every minute. Every alarm treats missing data as not
  breaching.
- **Ops module, runbooks, flags and the probe**: runbooks R7, R9 and R12-R17, one allow-listed state machine each
  (`<prefix>-runbook-<id>`), started by their alarms, `Channel Alert`, `Asset Failed`, `Schedule Published` or an
  hourly schedule; each execution appends its decision to the ledger chain `runbook/<id>`. Every runbook runs its
  dry-run path: it records what it would do. The AppConfig profile `flags` (feature flags) holds the
  kill switches. One CloudWatch Synthetics canary, `<prefix>-probe`, plays the running IaC channels every 5
  minutes; the census starts and stops it, and two failed runs in a row start R12.
- **Shared as-run router** (`observability.as_run`, ADR-0032): `shared` deployments read their channels' as-run
  events from the account's default event bus instead of subscribing to the as-run log themselves.
  `make as-run-router` deploys the account's router stack `FastChannels-AsRun` once per account and region: one
  subscription, every event to the bus with its `as_run_id`, alarms and a dead-letter queue, termination protected.
  `make deploy` and `make destroy` never select it. The default `per_stage` is v3.0's. The `fastm` and `fastci`
  stages are `shared`, so development and test stages no longer use the log group's five subscriptions.
- **Model registry** (`modules.models`): the `FastChannels-<prefix>-Models` stack renders one validated SSM entry per
  LLM task, `/<prefix>/models/tasks/<task>` (schema `contracts/model-registry.schema.json`), from
  `modules.models.tasks`, with models only from `agent.allowed_models` and viewer-data tasks never on a `global.`
  profile. The scheduler agent reads its `scheduler.plan` entry first (60 s cache), then the ModelSpec, then the code
  default, and logs the task, model and source of every run. A spec or entry may set `thinking` (`adaptive` or
  `disabled`).
- **Per-task inference profiles and caps** (`modules.models`): each task gets an application inference profile
  `<prefix>-<task>` (tagged `fast:task`, its own `AWS/Bedrock` metrics and cost line), named in its registry entry;
  the scheduler agent invokes only its profile, and its role may invoke nothing else. A deterministic daily spend
  counter per task (`<prefix>-models`) refuses runs and calls past `daily_cap_usd` with `MODEL_CAP_REACHED` and tells
  the alert topic once a day; `scheduler.plan` ships with a $5 cap. `budgets_action` adds an AWS Budget per task role
  (monthly, at 31 days of the cap: Budgets actions accept no daily budget) whose action denies the role's invocations.
- **Eval records and the synth gate** (`modules.models`, ADR 0029): `make eval TASK=<task> MODEL=<id>` runs the
  task's conformance suite with its settings and writes `evals/records/<task>/<key>.json`
  (`contracts/eval-record.schema.json`). Synth refuses a task whose `eval` names no passing record for its model and
  settings; the `scheduler.plan` record of Claude Haiku 5.5 is committed. The recommender's record format, with its
  deterministic faithfulness check, is defined. With the registry on, the scheduler's ModelSpec parameter is retired:
  the agent falls back to its entry as deployed.
- **Demand: the pinned Prebid Server guidance** ([docs/demand.md](docs/demand.md), ADR-0020, ADR-0002 exception A).
  `vendor/prebid-guidance/PIN.toml` pins the AWS Guidance for Deploying a Prebid Server on AWS at v1.4.0, with the
  SHA-256 of every upstream file its overlays patch or rely on. Five overlays add a stack-name prefix and a dedicated
  CDK bootstrap (O1), a customer WAF rule group (O2), the fast-ctv module's build hook (O4), simulator creatives from
  configuration (O5) and 100% minimum healthy ECS deployments (O7). `scripts/prebid_guidance.py checkout|verify|apply`
  refuses a moved tag, a changed upstream file or a patch that does not apply, and the CI job `guidance-overlays`
  applies the overlays on every run. `render-config` renders a stage's Prebid Server configuration
  (`fast_channels.core.pbs_config`): one stored request per route (`fast-pod`, and `fast-pod-kids`), a template pod for
  the own book only that the fast-ctv module shapes to the break, the stage's account with the module's execution plan
  and Activity Controls, and the application configuration.
- **Demand: the fast-ctv Prebid Server module** (`pbs/fast-ctv`, [docs/demand.md](docs/demand.md#the-fast-ctv-module),
  ADR-0001 amended). A policy hook adds third-party demand only on a positive, in-time `/policy` answer from the Pod
  Decision Service (contract version 1, strict both ways; never in `SAFE` or `KIDS` breaks), and an exitpoint hook
  renders the pod `/select` returns as one VAST 4.2 document for MediaTailor ad buffet, with our impression tracker on
  every ad and a house-only fallback. A break without a valid session, configuration, event id or duration is not
  asked about. `scripts/pbs_module.py` builds Prebid Server Java with the module exactly as the image does
  (pinned commit, Maven 3.9.11) and runs its JUnit tests, which ask and render every example
  in `contracts/fixtures/pds/`; `tests/pbs` (marker `pbs`) runs the no-hook contract matrix, header isolation, wire
  fields and the impression rule against the jar, and validates every call it makes against the contract's models.
  New CI job `pbs-module` and make targets `pbs-test`, `pbs-contract`.
- **Demand: deploy and destroy** ([docs/demand.md](docs/demand.md#deploy-and-destroy)). With `modules.demand` on, the
  stage gains `FastChannels-<prefix>-DemandEdge` (us-east-1: two origin secrets and the WAF rule group the
  guidance's web ACL references first, which blocks `/openrtb2/*` without a secret, every other path but
  `/openrtb2/auction` and `/status`, and IPs above `rate_limit_per_ip`) and `-DemandBuild` (the image repository,
  an x86 CodeBuild project, the guidance's CloudFormation execution policy). `make deploy-demand` /
  `scripts/prebid_guidance.py deploy` then creates the guidance's own CDK bootstrap (qualifier = prefix, never the
  shared `hnb659fds` roles), builds the image, deploys the guidance (`SpotInstanceWeight=0`, task limits from
  `modules.demand.tasks`, the rule group, the simulator per mode), writes the SSM contract `/<prefix>/demand/*` and
  runs a smoke test; `make destroy-demand` removes all of it, including what the guidance's templates retain, and
  prints the inventory. The demand module needs a prefix of at most 10 characters (the bootstrap qualifier).
- **Demand: the bidder simulator and the demo creatives**
  ([docs/demand.md](docs/demand.md#demo-demand-and-the-bidder-simulator)). In `modules.demand.mode: demo` every ladder
  kit also encodes ten 30.03 s demo creatives from the brand stills in `assets/demo`: `sim1`–`sim8` for the simulator
  (`sim3` and `sim4` share an advertiser) and `direct1`–`direct2` for the own book's direct lines. The stills are CDK
  file assets, which MediaConvert reads object by object, so nothing is written to the ingest bucket. The kit indexes
  them in `ads/<ladder>/demo/creatives.json`. `deploy-demand` renders the simulator's creatives from that index (overlay
  O5) for either mode: the guidance's simulator stack (`guidance_stack`), or the same handler as a Lambda target of an
  internal load balancer in the Prebid Server VPC (`lambda`, `sim.<prefix>.fast.internal`). The new stack
  `FastChannels-<prefix>-Demand` holds the stage's private zone `<prefix>.fast.internal` and the `lambda` simulator.
  `deploy-demand` deploys it once the guidance is up, and `destroy-demand` deletes it first. The smoke test also sends
  the simulator a 4-slot request and checks every demo creative's renditions against the ladder.
- **Demand: census-gated scaling and warm-up**
  ([docs/demand.md](docs/demand.md#scaling-the-census-gate-dayparts-and-warm-up)). Prebid Server runs only while the
  stage airs. The census gate (Step Functions, SDK calls only) raises the guidance's ECS service to `tasks.min` when the
  census's `Census Changed` event reports the first running channel, and scales it to zero 15 minutes after the last one
  stopped, unless a channel started again. `modules.demand.dayparts` become Application Auto Scaling scheduled actions.
  Every new task, from any scale-out, gets a warm-up once it is a healthy target: probe auctions without a ticket
  through the guidance's CloudFront, at 25/s per healthy target for 150 s, with a pinned, checksum-verified vegeta on
  CodeBuild. Undeliverable events go to a dead-letter queue that alarms. `prebid_guidance.py herd` replays a break herd.
- **Demand: Prebid Server Java 4.4.0** (`core.pbs_config.PBS_VERSION`, ADR-0020). The image builds 4.4.0 on
  Corretto 25: guidance overlay O6 makes the JDK a build argument, and O4 ports the guidance's `amt` adapter
  configuration to 4.x. The rendered configuration adds the two settings 4.x requires. CI builds and tests the module
  and the contract on 4.4.0 (`make pbs-test pbs-contract`). The OpenRTB 2.6 content fields reach bidders
  first-class. The guidance's log analytics stays off: its reporter logs every request header, the ticket and the
  origin secret included. See [docs/demand.md](docs/demand.md#the-fast-ctv-module).
- **Demand: the e2e demand leg**: the `End-to-end` workflow's `leg: demand` (job `e2e-demand`, up to 4 hours)
  deploys `STAGE=fastci MODULES=ops,demand,pds,release,crawl`, runs `tests/e2e/test_11_demand_edge.py` to
  `test_14_demand_pods.py` (below), then destroys and inventories everything. `test_11` covers the break lengths
  from 30 to 120 s and the empty pod of one outside them, the origin rule group, an account without the module
  calling no third party, and a small herd. `MODULES=a,b` (`stage_args.py --modules`, `prebid_guidance.py
  --modules`) keeps only those modules of a stage on. `deploy-demand` also warms the running tasks no warm-up build
  has named (a first deploy starts them before the warm-up rule exists) and waits for them.
- **Prebid results for an ad server's VAST, experimental** (`modules.demand.ad_server`, ADR 0035,
  [docs/demand.md](docs/demand.md#an-ad-server-in-front-prebid-results-for-its-vast)): with `mock`, `gam` or
  `freewheel`, the pair siblings ask an ad server for the break instead of Prebid Server. The mode is off by default
  (`none`), and no default end-to-end leg runs it.
  - A PRE_ADS Function runs the break's auction once per session and break (scope `session_break`), through the
    origin rule. The Release Controller adds the origin secret when it creates the Function.
  - The fast-ctv module stores the composed pod in the guidance's Prebid Cache, through Prebid Server's own cache
    client. It answers the pod's `hb_pb` (CTV price buckets), `hb_uuid` and `hb_bidder`.
  - The ad server's Prebid line answers a VAST Wrapper to `/cache?uuid=<hb_uuid>`, and MediaTailor stitches the pod.
  - Prebid Server writes to the cache inside the VPC (`cache.<prefix>.fast.internal`). From the internet, the origin
    rule group lets only `GET` and `HEAD /cache?uuid=<uuid4>` through.
  - The guidance's Prebid Cache function gets 1,769 MB and 8 instances of provisioned concurrency
    (`pod_cache_memory_mb`, `pod_cache_concurrency`), through the guidance wrapper. The write is on the auction's
    deadline. At the guidance's own settings, a 100-session break lost 17 of its 100 pods; with these, it lost
    none. They cost about $0.17 an hour, about $124 a month, only while the mode is enabled: always-on, as
    scaling them with the channels was deferred.
  - `mock` deploys a GAM-like ad server that answers only requests with the origin secret. `gam` and `freewheel` are
    configuration and setup instructions; they are not tested live.
  - A rotation of the origin secret creates every kept auction tree again with the new value, under a new release.
    It does not retire the old value while a Function a sibling keeps still carries it.
  - The `fastciads` stage runs the end-to-end leg. With `none`, the default, nothing changes.
- **Pod Decision Service rules** (`fast_channels.core.pds`, [docs/pds.md](docs/pds.md)): the versioned `/policy` and
  `/select` contract with the Prebid Server module (`contracts/pds-*.schema.json` and shared examples under
  `contracts/fixtures/pds/`), privacy modes with every viewer parameter zeroed on an identity failure, the
  right-to-left viewer-IP rule that ignores client-set X-Forwarded-For entries, a User-Agent device-type map, pod
  selection (tier-0 lines first, accepted net value, cross-bidder creative, advertiser and category separation, pods
  that fit the break, prefetch rounding, standalones for ad buffet, empty pods for avails under 30 s), trackers,
  decision records, deterministic buckets and the ad-load price stub.
- **Pod Decision Service** (`modules.pds`, [docs/pds.md](docs/pds.md#the-service), ADR-0036): `POST /policy`,
  `POST /select` and the own book run as a sidecar in each Prebid Server task, on `127.0.0.1:8090`.
  - The sidecar is the workspace member `pds`: uvicorn over the same pure `core.pds` rules, its endpoints on a bounded
    thread pool, shedding what its callers stopped waiting for. Its image is built for x86 into `<prefix>-pds`.
  - `scripts/guidance_app.py` adds it to the guidance's own stack without a patch: the container, which Prebid Server
    waits for, 6 GiB of task memory, its grants, its S3 reads in the VPC's gateway endpoint policy, a DynamoDB gateway
    endpoint for its tables, and ALB slow start for new tasks.
  - `FastChannels-<prefix>-Pds` keeps the snapshot publisher (the demand allow-list and CloudFront's origin-facing
    networks in a private config bucket), delivery, the sweeper, the fallback alarms and the decision records'
    streams into the lake (`pds_policy`, `pds_select`, from the sidecar's log group).
  - The Data stack holds the module's table `<prefix>-pds` and the config bucket. `deploy-demand` deploys the stack
    after the Demand stack, and `destroy-demand` deletes it first.
  - In 400-session break herds the module measured `/policy` at p99 12-18 ms and `/select` at p99 65-134 ms with
    no fallback, so `modules.pds.policy_timeout_ms` is 50 ms again (group 75) and `select_timeout_ms` stays 200 ms,
    bound by the reservation's DynamoDB transaction. The demand module's warm-up replays 25 auctions/s per healthy
    target (it was 50).
- **Own book, reservations and delivery** ([docs/pds.md](docs/pds.md#the-own-book), ADR-0022):
  `POST /ownbook/openrtb` bids the default ladder's house ads at $0.01 (`ext.fast.house`) and the creatives of each
  line with budget left (`modules.pds.demo_lines`, now set on `fastm` and `fastci`) at their CPM, a tier-0 line with
  its deal id. Reservations take a line ad's price from one of four budget shards in one transaction with the session
  cap, the reservation and its ledger money event, so liability never exceeds the budget. A subscription filter on the
  stage's MediaTailor log group counts our tracker's `BEACON_FIRED` impressions once per `(sid, evid, crid)` and fills
  once per `(sid, evid)`, delivers their reservations and sends `pds.delivery` records to the lake (`pds_delivery`); a
  sweeper releases reservations without an impression after two hours.
- **`POST /select`** ([docs/pds.md](docs/pds.md#pod-rules)): the pod rules on the snapshot (tier-0 lines first, the
  floor, the break of record for prefetch rounding), every own-book line ad in the pod or among its standalones
  reserved before it is answered (a line that cannot be is left out, `unreserved`), our tracker on every ad, and a
  `pds.select` decision record into the lake (`pds_select`). Alarms on Prebid Server's log: more than 1% of breaks
  without the `/policy` patch, more than 0.5% of pods from the module's house fallback. The shared `/select` examples'
  house bids carry no `adomain`, as the own book's.
- **Monetization desired state** (`fast_channels.core.monetization`, for the release module):
  MediaTailor Monetization Functions as `PutFunction` documents with immutable `<p>_<name>_rNNNN` ids, the
  PRE_SESSION session controls (mid-break joins see slate, server-side beacons, no debug log, no pre-roll), the
  sibling playback configurations `<prefix>-<ch>-p-<s>` with their templated OpenRTB POST to Prebid Server
  (threshold 121, ad buffet, no player value in the body), and the release bundle with its digest and a
  contribution interface for other modules' function trees. Validators R-F1 to R-S1 and R-A1/R-A2 check the
  documented Functions limits and the JSONata allow-list that MediaTailor does not enforce when a function is put,
  the body template, the cache keys, the channel-policy pattern and the rollout strategy. The bundle's JSON Schema
  is `contracts/monetization-bundle.schema.json`.
- **Demand: the MediaTailor-driven smoke** (`tests/e2e/test_14_demand_pods.py`, in the demand leg). It waits until
  Prebid Server is warm (the PDS is its sidecar), airs a 30.03 s break and two of 120.12 s, and plays four viewers on
  the stable sibling of the channel's playback plan, two late joiners after the second break's retrieval closed, and
  one viewer 40 s into the second break. From the siblings' ADS log, the module's lines, the PDS's records and the
  simulator's log it checks that:
  - every ad server request (a prefetch retrieval or a real-time request) is one auction, answered with XML and
    rendered from `/select`;
  - a retrieved break is filled from its retrieval, with no second request and none of its winners skipped as not
    ready;
  - a late joiner is asked in real time and served, and a mid-break joiner is asked for the rest of that break
    (ADR-0021): slate for what aired, then its pod, every ad from its first segment;
  - pods are stitched in `sequence` order; where a winner was new at its break it is replaced in its position by a
    standalone, and a partial pod is kept at threshold 121 with the break slate after it (with prefetch these skip,
    saying why);
  - the decision records carry MediaTailor's session id; no client header reaches the simulator, which gets the
    viewers' address masked and never whole;
  - delivery is counted from `BEACON_FIRED`; crawl covers at least 99% of the enriched arm's breaks;
  - no requested break is empty unless nothing was eligible, and none falls back; the stable sibling's silent-loss
    alarm never fires.

  Overlay O5's `sim-bid-request` line has the X-Forwarded-For entry count and `device.ip`/`device.ipv6` as the
  request body carries them.
- **Static channel-policy pattern** for the release module: a channel in `modules.release.channels` also admits
  its sibling playback configurations by `ArnLike` `<channel>-p-?`, set when the channel is created, and so does
  every agent channel created while the module is on. The channel update guard treats adding the pattern as a
  change, so it refuses it on a running channel (or stops and restarts the channel with `restart_on_update`),
  because a policy change on a running channel is not session-safe. With the module off the policy is unchanged.
- **Release Controller: the apply path** (`modules.release`, `FastChannels-<prefix>-Release`,
  [docs/release-controller.md](docs/release-controller.md)). CDK renders and validates the release bundle;
  `Custom::DesiredState` stores it by digest and puts `Desired State Rendered` on the bus; the state machine
  `<prefix>-release` validates it again and, one release at a time, creates the MediaTailor Functions its siblings map
  (an id is never updated), configures the idle sibling `<channel>-p-<s>` (all siblings on a channel's first release)
  with the origin secret read at apply, creates its vended-log deliveries without the fields that may hold the secret or
  a ticket, rewrites `/plan/playback/<channel>.json` and ledgers the release. The siblings' log group has its own KMS
  key, whose policy lets only named roles decrypt. An hourly drift check (alarm `<prefix>-release-drift`), per-sibling
  hook alarms and a zero-ad-sessions detector watch the siblings. With the module on, every other role of the stage is
  denied the writes the controller owns (managed policy `ReleaseGuard`), and the controller never changes a channel
  policy. `deploy-demand` deploys the stack after Pds; deleting it reaps what the controller created. The Media stack
  writes the SSM parameters `/<prefix>/release/*` it reads. The Pod Decision Service's delivery function also counts the
  siblings' impressions, through a subscription filter on their log group.
- **Release Controller: smoke gate, promotion and rollback** (`modules.release`,
  [docs/release-controller.md](docs/release-controller.md#promotion-and-rollback), ADRs 0019, 0021, 0023). After a
  release configures a channel's idle sibling, a probe session smoke-tests it for two breaks and soaks it for three
  more (on the lab channel `<prefix>-lab`, started through the agent tools, when no monetized channel runs); the
  release pointer (AppConfig profile `release`) is deployed with the stage's strategy, never all at once, and the
  environment monitors the composite alarm `<prefix>-release-candidate`; at 100 % the candidate becomes stable and
  its plan is rewritten. A failed smoke or soak, a rolled-back pointer, or the candidate alarm outside a promotion
  runs R1: the sibling is remapped to its last good tree and the plan names the stable sibling. Per-sibling metric
  filters on the siblings' log group count each break's first ad marker, ad requests and empty responses, and
  `<sibling>-markers-without-ads-request` fires when more than half of the markers had no request in 2 of 10 minutes
  (an invalid rendered body sends no request and logs no error). A PRE_ADS request that times out inside an executor
  is no Function or hook error, so the smoke judge and the pointer poll read each request's `Function.Latency`
  against its own timeout from the release's bundle: at the timeout in two minutes, the smoke fails or the pointer
  deployment is stopped, and R1 runs. Each sibling keeps three Function trees; older ones are deleted.
- **Every Function request is cached by scope** (`modules.release`, ADR 0034,
  [docs/release-controller.md](docs/release-controller.md#function-caches)): each `HTTP_REQUEST` in the release
  bundle declares a cache scope (`title`, `break`, `session`, `session_break`, `global`, or `none` for a single-use
  call) and the reason for it. Validator R-F8 refuses a request without one, a cacheable scope without `Cache`, a key
  that does not name exactly its scope's identifiers (a `break` key also names its channel) and a TTL that outlives
  the scope. R-F4 now admits `session.uuid` in session-scoped keys. The crawl lookup is `title`. Scope and reason
  stay in the bundle and are not sent to `PutFunction`.
- **Ad prefetch** (`modules.release.prefetch`, on with the release module; ADR 0033,
  [docs/release-controller.md](docs/release-controller.md#prefetch)): the Release Controller's prefetcher keeps one
  SINGLE prefetch schedule per upcoming break (the next 20) on each general and kids sibling of every running monetized
  channel, built from `GetChannelSchedule` and `DescribeProgram`, matched on `scte.event_id`, with rule R-S1's dynamic
  variables and `session.avail_duration_secs`. Retrieval runs 150 s to 30 s before the break with `RETRIEVAL_WINDOW`
  traffic shaping over 90 s, so the ad request herd leaves the marker and new creatives are prepared before they air.
  Schedules are create-only: those of moved or re-planned breaks are deleted, a sibling with a RECURRING schedule is
  refused (alarm `<prefix>-release-prefetch`), and the reaper deletes them with the siblings. A break has moved only
  when its start is more than 3 s from its schedule's (in whole seconds); a smaller shift of Channel Assembly's start
  keeps the schedule and its retrieved ads, and a run over an unchanged schedule writes nothing.
- **Release log reconciliation** (`modules.release`). Every hour, each sibling's vended ADS log is compared with
  `AWS/MediaTailor`'s own count of filled ad time (the `Sum` of `Avail.FilledDuration` against the log's
  `FILLED_AVAIL` durations); `<prefix>-release-log-loss` fires when MediaTailor filled ad time the log does not
  have. MediaTailor was seen leaving a requested, filled break out of its log; delivery, which reads that
  log's beacons, would under-count it.
- **Mid-break joiners ask for ads** (ADR-0021, amended): the session controls and the siblings' `AvailSuppression`
  are `AFTER_LIVE_EDGE` `00:00:00` with `PARTIAL_AVAIL`, so a viewer who joins during a break gets a request for
  what is left of it, and each ad inserted plays from its first segment; before, the slate played for the whole
  break. MediaTailor asks for the remainder at segment granularity. On the Prebid Server route a remainder from 30 to
  120 s is filled, one 30 s slot per whole 30 s of it (the one stored request per route), and a shorter one gets an
  empty pod, so the slate plays.
- **Origin-secret rotation** (`modules.release.secret_rotation_days`,
  [docs/release-controller.md](docs/release-controller.md#origin-secret-rotation)). The state machine
  `<prefix>-release-rotation` rotates the demand gate's origin secret with two values admitted at once: a new value
  for the slot the siblings do not send, admitted at the WAF beside the current one; a probe through the guidance's
  CloudFront until it is admitted; every sibling re-put with it; one break verified; then the old value replaced by
  one nobody has seen. It holds the Release Controller's lock (one rotation at a time), rotates every
  `secret_rotation_days` days (a daily EventBridge Scheduler check) and on demand, and raises
  `<prefix>-release-rotation-failed` on a failure.
- **Show-level content metadata** (the `crawl` module): optional catalog fields for series, season,
  episode, content type, language, producer, keywords, production quality and external ids (existing catalogs are
  unchanged); the catalog context object `catalog/ctx/v1/<content_id>.json` that ad requests carry, with its
  contract `contracts/catalog-context.schema.json` and the documented Functions limits enforced; and catalog genre
  tags mapped to IAB Tech Lab Content Taxonomy 3.1 through the IAB CTV Genre Mapping (`genre-map.toml`, unknown ids
  refused). The sample catalog names each title's producer, language and type. See
  [docs/content-enrichment.md](docs/content-enrichment.md) and ADR 0031.
- **Catalog context publisher** (`crawl` module): with `modules.crawl.enabled`, the stack `FastChannels-<prefix>-Crawl`
  keeps one context object per reference-catalog title in a catalog-context bucket of its own (`catalog/ctx/v1/`,
  served on a CloudFront route `/catalog/ctx/*` with `Cache-Control: max-age=300`). The bucket lets that distribution
  list it, so a title missing from the catalog is a 404, which MediaTailor's Function cache stores; the media bucket
  and its policy are unchanged. It runs on `Schedule Published`, on a new `Catalog Item Changed` event that a
  CMS sync job puts after changing a title, and in a daily sweep. It rewrites an object only when its content
  changes and deletes the objects of removed titles. `modules.crawl.publisher_domain` names the registry of your
  content ids. `make seed` now announces the sample titles with `Catalog Item Changed`.
- **Cached PRE_ADS content lookup** (`crawl` module, with `modules.crawl.pre_ads`): the release bundle maps a PRE_ADS
  root executor (400 ms) on every general sibling. Its one cached `HTTP_REQUEST` (250 ms, 300 s keyed on the break's
  SCTE-35 UPID) reads the title's catalog context object and sets the `x-fast-ctx` header to its `hdr`, capped at
  1,000 characters. It reads no session or player value and fails open.
- **Content metadata in bid requests and the uplift holdout** (`crawl` module): the Pod Decision Service's `/policy`
  answer fills OpenRTB 2.6 `app.content` from a copy of the catalog context objects (refreshed every 30 s, with the
  `x-fast-ctx` header for a title the copy lacks), the OpenRTB 2.6 fields first-class. PSEUDONYMOUS breaks get no field
  that names the title, SAFE breaks none. A `crawl-v1` holdout (`modules.crawl.holdout_percent`, 20 %) gets no
  enrichment; the arm (`enr`) and where the metadata was found (`ctx`) are in the `pds.policy` record, and the arm in
  our impression tracker. The lake view `v_crawl_kpis` reports coverage, bids, fill and price by hour, playback
  configuration and arm.

### Changed

Descoped before the release (the owner, 2026-10-30; each is a two-way door):

- **Prebid Server Java 4.4.0 only.** The demand module builds 4.4.0 and nothing else: no release switch, no build or
  CI job for the 3.43.0 the guidance ships.
- **One stored request per route** (`fast-pod`, `fast-pod-kids`, and the ad-server route's) instead of one per break
  length: the POST body sends the break's length (`ext.fast.pod_s`) and the fast-ctv module shapes the pod. A stage
  deployed from the pre-release branch upgrades in this order: `deploy-demand` (it uploads the new stored requests
  and deletes nothing), the Release stack's rollout (every sibling sends the new body), then `upload-config --prune`,
  which deletes the per-length `fast-pod-<n>s` objects except any a live configuration still names
  ([docs/demand.md](docs/demand.md)).
- **The ad-server mode is experimental** and off by default (`modules.demand.ad_server: none`); no default
  end-to-end leg runs it.

### Fixed

- **Demand: the deploy smoke on a stage with nothing on air.** With no channel running, the census gate holds Prebid
  Server at 0 tasks, so `deploy-demand`'s synthetic POSTs met a 503 and the deploy reported a failed smoke. The smoke
  now checks the service first: at a desired count of 0 it sends nothing, prints `smoke skipped: 0 running Prebid
  Server tasks (no channel on air; census gate)` and records `skipped` (never `passed`); tasks that are desired but
  not running still fail it.
- **Demand: a sub-second real-time herd is named and sized.** When MediaTailor asks for every session of a
  prefetched sibling at the marker at once, about 50 auctions a task in half a second outrun the task's 2 vCPU and the
  module serves those breaks SAFE (measured: 64-71 of 100 on 2 tasks; 0 at 25 auctions/s per task). The Release stack
  counts the siblings' real-time ad requests (`RealTimeAdsRequests`), and the composite alarm
  `<prefix>-pds-burst-real-time` fires when the PDS's policy fallbacks and a mostly real-time sibling coincide.
  docs/demand.md sizes Prebid Server from a break's peak arrivals, and ADR-0036 records the evidence: the capacity
  line, two sidecar processes tried and not adopted, and the reservation-hold measurements.
- **Demand: a changed setting reaches an existing stage.** The guidance uploads its configuration directory only when
  its stack is created, so `deploy-demand` on an existing stage left Prebid Server on the old configuration (seen
  live: the module's timeouts stayed at their old values in S3). `deploy-demand` now syncs the rendered
  configuration after every guidance deploy: it uploads what differs from the guidance's settings and stored-requests
  buckets, reads every object back and refuses a mismatch, lists what an earlier configuration left, and rolls
  the tasks only if something changed. `upload-config` runs that step alone. With `--prune` it also deletes the
  stale objects (under the managed prefixes only, keeping the guidance's own files, each listed first), except the
  stored requests a live playback configuration or Function still names, which it reads from MediaTailor first: a
  sibling of an earlier release never loses the stored request it asks for
  ([docs/demand.md](docs/demand.md#deploy-and-destroy) gives the order for a body-format change).
- **Demand: a break the PDS could not answer still gets a pod**. When `/policy` timed out, the module served
  the break SAFE (own book only), but with the own book silent too the pod was empty and the slate aired: on fastm 3.8%
  of pods hit a policy timeout and 69% of those came back empty. Now, if such a pod would be empty, or `/select` is
  unavailable and the auction has no house ad, the module renders the kit's house ads from its configuration
  (`hooks.fast-ctv.house-ads`, which `deploy-demand` renders from the default ladder's `vast.xml`), each with the
  module's own tracker. The `fast-ctv-pod` line says `"safe": true` and ends its `select` reason with `HOUSE_CONFIG`,
  and a metric filter counts those pods (`HouseFromConfig`). A pod `/select` chose empty with the policy applied stays
  empty, and a paid line never airs without a reservation.
- The as-run log group allows five subscription filters per log group, not two: at most five `per_stage`
  deployments can exist per account and region (docs/channel-assembly.md).
- **Demand: several guidance deployments per account and Region.** Overlay O1 now also prefixes the guidance's
  MetricsEtl Glue database (`<prefix>-prebid-server-<region>-metricsetl-database`). The shipped name ignores the
  stack, so a second stage's guidance (another lane's e2e leg next to `fastm`) failed with `Database already exists`.
  It was the only such name in the synthesized templates.
- **Demand: a redeploy with a new Prebid Server image now runs it.** The guidance takes the image as the default of its
  `ContainerImage` parameter, and `cdk deploy` kept the parameter's previous value, so a changed image (a module change
  or a release switch) left the old one running. The guidance deploy no longer reuses previous parameter values.
- **Release Controller: the lab channel's block has enough breaks for the smoke test and the soak.** It had two
  programs, and assets with editorial cue points break only at them: on fastm, a block of four breaks left the soak
  with one of its three, and the release rolled back. The block now takes programs until it has at least seven
  breaks (the smoke's two and the soak's three, a break the smoke's session joins and one during its log delay).
- **Release Controller: a held release run again after an origin-secret rotation** is judged, promoted and rolled
  back as the release its candidate runs. A rotation can create a tree again under a new number (`recreated_from`).
  The timed-out check used to read the old number's Functions, the promotion marked the old number, and the
  rollback left the candidate where it was.
- **Release Controller: prefetched breaks in the logs.** A prefetched break's ADS log events (its retrieval, its
  marker and its fill) carry `prefetchScheduleName` and no `adBreakCorrelationId`. The smoke and soak judge counted
  such breaks as unlogged and failed a healthy release on a prefetched sibling. Now they group events by
  `coalesce(adBreakCorrelationId, prefetchScheduleName)`. The silent-loss detector leaves prefetched markers out:
  their request was the retrieval, minutes earlier, so with five or more sessions their break minutes would have
  raised `<sibling>-markers-without-ads-request`. The log reconciliation and the smoke test still cover their fill.
- **Release Controller: the probe sibling follows the stable release.** Only a channel's first release configured
  `-p-z`, so the canary kept that release's session controls, trees and ADS body. Once the per-length stored
  requests were removed, every probe request got a 400 and the slate. Every promotion and rollback now re-applies
  the probe with its stable sibling's release (house only, `X-Fast-Probe`), and its earlier trees are deleted once
  no sibling keeps them. A probe that can't follow raises `<prefix>-release-probe-stale`, and the promote function's
  `probes` step catches up a stage by hand.
- **Release Controller: a broken ad route raises an alarm.** No alarm counted a monetized sibling's
  `AdDecisionServer.Errors`, so a route that fails every request (a missing stored request, Prebid Server down,
  the origin gate refusing) played the slate and lost the revenue silently. `<sibling>-ads-errors` fires when more
  than half of a sibling's ad requests fail in two consecutive 15-minute periods.

## [3.0.0] - 2026-10-16

A rewrite of FAST Channels on AWS on the AWS CDK (Python), with agentic scheduling on Amazon Bedrock AgentCore, one
plain HLS encode per asset (MPEG-TS, AVC with AAC muxed, MediaConvert Basic tier), frame-accurate ad cues, SSAI, and
end-to-end traceability. v2 deployments are not upgraded in place; see
[docs/migration-from-v2.md](docs/migration-from-v2.md).

### BREAKING

- The AWS SAM templates (`deployment/`), the SAM Lambda functions and layers (`source/`) and the CloudFormation
  parameters are gone. Deploy with `make deploy`; configure in the `fast-channels` block of `cdk.json`.
- AWS Elemental MediaPackage VOD is no longer used. Channel Assembly reads the encode's HLS manifests from S3 with
  SigV4. Content encoded or packaged by v2 cannot be scheduled on v3 channels and must be re-encoded.
- Stack, bucket and resource names, playback URLs and outputs are new.
- The default ladder (`standard`) is H.264 with AAC muxed, up to 1080p. v3 has no HEVC, 4K or DASH output.
- Ads are preconditioned by default (MediaTailor `AdConditioning` `NONE`).
- The per-asset notification email with playback URLs is replaced by EventBridge events and failure alerts.
- Python 3.14 on arm64 and Node.js 24 are required.

### Added

- **Operator plane on Amazon Bedrock AgentCore**: a Strands scheduling agent on AgentCore Runtime (AG-UI protocol,
  Cognito JWT authorizer), an AgentCore Gateway (MCP, CUSTOM_JWT) with 27 tools in six Lambda targets (`catalog`,
  `library`, `origins`, `schedule`, `channels`, `encoding`), a Cedar policy engine in `ENFORCE` mode generated from
  the tool registry, human approval for the 11 tools that change what airs or cost money, AgentCore Memory (sessions
  and operator preferences, tool traffic excluded from extraction), and agent telemetry.
- **Operator console** (React, Cloudscape, hls.js): chat with tool activity and approval cards; an SSAI player with
  ad events; EPG timeline; channel start and stop; library and ladders.
- **Autopilot**: per-channel EventBridge Scheduler schedules that keep a channel's schedule 24 hours ahead with a
  machine token limited by Cedar to planning and publishing on its own channels. The machine client, its permits and
  the autopilot's error and dead-letter alarms exist only when a channel has an `autopilot_cron`.
- **Model choice**: Claude Haiku 5.5 by default, as the deployment region's inference profile (`{geo}` resolves to
  `us.`, `eu.`, `au.` or `global.`); a validated ModelSpec in SSM switches among allow-listed models without a
  redeploy; `make model-access` checks Bedrock access; `make eval` runs the scheduling conformance suite.
- **Ladders as code**: validated, fingerprinted TOML ladders (`standard`) compiled to MediaConvert job templates;
  agent-driven ladder proposals (cap or floor the height, shift quality, drop rungs) and provisioning; encode cost
  estimates.
- **One plain HLS encode** per asset: a MediaConvert Apple HLS output group of MPEG-TS segments, each variant H.264
  High with AAC-LC 128 kbit/s stereo muxed in (`avc-720p` first, `avc-1080p`, `avc-360p`), 29.97 fps, 2 s closed GOPs,
  6.006 s segments and `CLOSED-CAPTIONS=NONE`, on the Basic tier at about $0.0375 per content minute. The encode's own
  `main.m3u8` is the one rendition profile, `hls`.
- **Frame-accurate ad cues**: cue points snap to the nearest frame; a point between segment boundaries adds ESAM to
  that title's job, which splits every variant at that frame and bills the job on the Professional tier (about $0.06
  per content minute).
- **Ingest pipeline** (Step Functions, JSONata): uploads with JSON sidecars, `Ingest Requested` events or tool calls;
  probe-based refusals (HDR, 50/60 Hz conversions); cue points snapped to the nearest frame; output verification
  against the ladder signature, nominal `BANDWIDTH`, break-offset discovery; VOD source registration. An encode that
  is `READY` is never overwritten in place, a reused correlation id is reported (`DuplicateCorrelationId`), and every
  refusal or failure ends in an `Asset Failed` event.
- **Ladder kits**: filler slate, four break slates, two house ads and a VAST 4.2 pod per ladder version, encoded at
  deploy time or by the agent.
- **Channels**: IaC LINEAR channels with per-channel `STITCHED_ONLY` playback configurations, SigV4-only channel
  policies, update guards and lifecycle handling; agent-created channels from the same specifications; auto-stop after
  a maximum runtime.
- **Schedules**: deterministic planning with programming briefs (ratings, genres, dayparts and a break policy whose
  minimum gap defaults to half the cadence, with start and end margins and a minimum program length that editorial
  cue points are exempt from), SCTE-35 `TIME_SIGNAL` breaks with deterministic ids, idempotent and resumable
  publishing with leases and stale-plan detection, every program `ABSOLUTE` at its planned start, the schedule of
  record in DynamoDB (kept 30 days after each program ends), EPG feeds in JSON and XMLTV, as-run reconciliation.
  Starting a stopped channel late removes the programs whose start has passed (reported as `skipped`, `SKIPPED` in
  the guide) and airs the rest at their planned times.
- **Bring-your-own content**: agent tools to create SigV4 source locations for your buckets, register pre-encoded
  content after a ladder-signature check, and update package configurations.
- **Delivery**: one CloudFront distribution (HTTP/2 and HTTP/3, IPv6, OAC) whose behaviours come from one routing
  table, published as a CDN-neutral path contract; optional CDN-neutral content domain; standard logs v2 in Parquet
  with CMCD fields.
- **Observability**: logs with retention for every service, one correlation id from upload to ad request, saved
  Logs Insights queries, `scripts/trace.py`, a dashboard, alarms (including Channel Assembly quota usage, running
  channel-hours and dead-letter queues) and SNS alerts; a `tool audit` record for every change made through a tool;
  optional CloudWatch Transaction Search for agent traces and online evaluations. Prompts and model replies stay out
  of the agent's logs and spans unless `observability.capture_prompts` is on.
- **Contracts**: catalog OpenAPI for bring-your-own CMS (also as `catalog.gateway.openapi.json`, ready for an
  AgentCore Gateway OpenAPI target), `IngestRequest` and event JSON schemas, generated from the domain models.
- **Teardown**: a runtime-resource reaper that deletes runtime-created resources before their containers, draining
  channels within each invocation's time budget so an interrupted teardown resumes; `scripts/cleanup.py` for
  teardowns that cannot finish.
- **Engineering**: uv workspace (`core`, `functions`, `agent`, `infra`), ruff, strict mypy (also over `scripts/` and
  `tests/e2e`), unit and CDK assertion tests, cdk-nag (AWS Solutions and Serverless) on every synth, a manual
  end-to-end release workflow, architecture decision records, Dependabot.

### Changed

- Encoding moved from a CSV-generated preset list to a validated ladder with 2 s GOPs, 6 s segments, QVBR and
  frame-rate normalisation.
- Ad-break placement keeps the v2 `AdOffsets` S3 object tag and adds sidecar and catalog cue points.
- Tracing uses Lambda and Step Functions active tracing and AWS Distro for OpenTelemetry instead of the X-Ray SDK.
- Documentation rewritten: README and `docs/` (architecture, encoding, Channel Assembly, ad insertion, delivery,
  agent, CMS integration, observability, operations, migration).

### Security

- The media bucket admits Channel Assembly only as the deployment's own source location, `<prefix>-media`, and the
  reaper's IAM reaches only `<prefix>-*` names, so one deployment's grants never match another's names (`fast` and
  `fastci`, for example).
- The autopilot's Cognito client, whose secret `cognito-idp:DescribeUserPoolClient` returns, exists only with an
  autopilot schedule, and its Cedar permits name the channels it schedules.
- The console accepts a local dev server as a sign-in redirect only with `agent.console_dev_url`.
- The Gateway's service role trusts only this deployment's Gateway; the alert topic admits no service principal
  (EventBridge publishes through a rule-scoped role); the agent runtime cannot read the CDK assets bucket beyond its
  own code.
- The end-to-end workflow validates its `prefix` input and passes it to commands only through the environment.

### Removed

- AWS SAM templates and the nested MediaConvert application.
- MediaPackage VOD packaging group and configurations, the Secrets Manager access token and the MediaTailor KMS key.
- The boto3, crhelper and xmltodict Lambda layers, and the SNS email sender function.
- `assets/presets.csv` and its preset generator scripts.

### Changed since the first v3.0.0 tag

- **Plain HLS MPEG-TS, AVC + AAC muxed, Basic tier**, for cost and stability
  ([ADR-0016](docs/adr/0016-plain-hls-ts-avc-aac-basic-tier.md)). The first tag encoded one CMAF group of AV1, HEVC
  and AVC with HLS and DASH manifests per rendition profile and IMSC1 captions, on the Professional tier at about
  $0.56 per content minute; every feature brought MediaConvert, MediaTailor or hls.js behaviour to work around. The
  encode is now three AVC variants with AAC muxed in, about $0.0375 per content minute. Gone: the `uhd` ladder,
  rendition profiles other than `hls`, DASH outputs and playback URLs, HEVC, AV1, audio rendition groups, I-frame
  playlists, accelerated transcoding, loudness normalisation, and captions (the `captions.*` settings, the Elemental
  Inference captioner, caption fields in requests, events and tools, and the console's CC toggle). Every ladder
  version changes: kits re-provision and assets re-encode.
- **SSAI only** ([ADR-0017](docs/adr/0017-ssai-only-frame-accurate-cues-with-esam.md)). The first tag served SSAI and
  SGAI (HLS Interstitials) from `PLAYER_SELECT` playback configurations; they are now `STITCHED_ONLY`. Gone:
  `?aws.insertionMode=GUIDED` URLs, asset lists, client-side beacons, the `/preconditioned-ads/*` and `/v1/i-media/*`
  CloudFront routes, and the console's SGAI player. Playback URLs (the `Channels` output, `channels___get_channel`,
  `core.specs.playback_urls`) are one SSAI URL per output instead of `{ssai, sgai}`. Preconditioned house ads stay:
  their manifest copies under `ads/` are the VAST `MediaFile`s, and MediaTailor stitches their segments through
  `/v1/segment` redirects. CloudFront follows MediaTailor's production configuration: one uncached `/v1/*` route
  (it replaces `/v1/manifest/*` and `/v1/*`) forwards every viewer header, including the `Accept-Encoding`,
  `User-Agent`, `Host` and `X-Forwarded-For` that MediaTailor requires from a CDN.
- **Frame-accurate cue points with ESAM** ([ADR-0017](docs/adr/0017-ssai-only-frame-accurate-cues-with-esam.md)).
  Cue points snap to the nearest frame (about ±17 ms at 29.97 fps) instead of the first tag's 2 s GOP grid. As in the
  first tag, a point between segment boundaries adds ESAM to that title's job only (`SccXml`, `ResponseSignalPreroll`
  0); on the MPEG-TS ladder it splits all three variants at that frame (verified at 13.480 s and 26.994 s) and the
  grid resumes. Such a job is billed on the Professional tier, about $0.06 instead of $0.0375 per content minute
  (`library___ingest` reports `pricing_tier`); job templates, kits and other titles stay on the Basic tier. Finalize
  keeps a cue point when a segment boundary of every variant lies within 100 ms of it.

### Fixed since the first v3.0.0 tag

- VOD source names are at most 50 characters (Channel Assembly's limit; 64 was assumed), so assets with ids longer
  than about 29 characters no longer fail registration.
- **Capacity guidance is now measured, not extrapolated**
  ([delivery-and-multi-cdn.md](docs/delivery-and-multi-cdn.md#capacity)). MediaTailor SSAI reuses Channel Assembly
  responses across sessions, and a load test of one channel up to 1,600 concurrent sessions leveled off near
  9 requests per second. The docs had said "about 300 concurrent viewers per channel"; that is replaced by the quota that
  actually binds, MediaTailor's Ad Insertion requests, at about 37,000 concurrent viewers per account at 70 % of default
  quotas. Updated: ADR-0007 and ADR-0017 consequences, the operations quota table, and the ad-insertion and
  Channel Assembly references.
- **The agent can create channels again.** `channels___create_channel` failed every time with
  `ForbiddenException: Cannot create the service-linked role`, and rolled back what it had created. MediaTailor's
  `ConfigureLogsForChannel` and `ConfigureLogsForPlaybackConfiguration` refuse a caller that may not create
  MediaTailor's service-linked role, even when `AWSServiceRoleForMediaTailor` exists, and the channels tool role
  could not. It now may create only that role (`iam:CreateServiceLinkedRole`, condition
  `iam:AWSServiceName = mediatailor.amazonaws.com`). IaC channels were not affected, because CloudFormation configures
  their logs with the deploying role. The end-to-end suite now creates and deletes an agent channel
  (`test_08_agent_channel`).

### Known issues

AWS service behaviour that Apple's mediastreamvalidator reports at Must level
([ad-insertion.md](docs/ad-insertion.md#monitoring-and-troubleshooting)):

- Channel Assembly removes a break's `EXT-X-DATERANGE` one playlist update too early: 552026 for most breaks on every
  live media playlist of the Channel Assembly origin and SSAI.
- MediaTailor SSAI writes the `EXT-X-PROGRAM-DATE-TIME` of the second and later ads in a pod one segment off in some
  reloads (536003).

## [2.0.0] - 2026-01-30

### Added
- 4K (3840x2160) encoding support with HEVC and AVC codecs
- 1080p (1920x1080) encoding support
- HEVC (H.265) codec support for improved compression at higher resolutions
- GitHub Actions CI/CD pipeline with lint, validate, build, and security scanning
- Dependabot configuration for automated dependency updates
- AWS X-Ray tracing for all Lambda functions
- HTTP/3 support for CloudFront distribution
- S3 Intelligent Tiering lifecycle policy for cost optimization
- KMS key rotation enabled for enhanced security
- SNS topic encryption with KMS

### Changed
- **BREAKING**: Upgraded Python runtime from 3.9 to 3.12
- **BREAKING**: Updated boto3 layer to 1.35.x (from 1.26.48)
- Updated crhelper to 2.0.11+ for improved CloudFormation custom resource handling
- Updated all Lambda functions with type hints and improved error handling
- Reduced IAM permissions to follow least-privilege principle
- Improved CloudFront security with TLS 1.2 minimum and HTTPS redirect
- Increased Lambda memory to 256MB for better cold start performance
- Increased Lambda timeouts for more reliable processing
- Added retry configuration with adaptive mode to AWS SDK clients
- Reduced segment duration from 8s to 6s for lower latency
- Enhanced S3 bucket security with encryption and public access blocks
- Improved logging with structured format

### Fixed
- Resource cleanup in custom resource delete handlers
- Error handling for duplicate VOD source creation
- Channel stop before delete to prevent deletion failures

### Security
- Restricted Secrets Manager policy from wildcard to GetSecretValue only
- Added specific resource ARNs to IAM policies instead of wildcards
- Enabled server-side encryption on all S3 buckets
- Added public access blocks to S3 buckets

### Removed
- Legacy Python 3.9 compatibility
- Overly permissive IAM wildcard policies

## [1.0.0] - 2023-02-01

### Added
- Initial Version

[3.0.0]: https://github.com/philhar88/AWS-Fast-Channels/compare/v2.0.0...v3.0.0
[2.0.0]: https://github.com/philhar88/AWS-Fast-Channels/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/philhar88/AWS-Fast-Channels/releases/tag/v1.0.0
