# Demand: Prebid Server through the AWS guidance

The optional demand module (`modules.demand`, off by default) puts an OpenRTB auction behind the channel's ad
breaks. MediaTailor POSTs a templated OpenRTB request to Prebid Server, deployed from AWS's [Guidance for Deploying a
Prebid Server on AWS](https://github.com/aws-solutions-library-samples/prebid-server-deployment-on-aws), and gets back
one VAST 4.2 pod. The decision is recorded in [ADR-0020](adr/0020-demand-through-the-prebid-server-guidance.md).

**Status (Phase 1, in progress):** the pinned guidance with its overlays, the Prebid Server configuration renderer,
the fast-ctv module, the origin gate, the image build, the deploy and destroy scripts, the bidder simulator and the
demo creatives, and the census-gated scaling with its warm-up are in place. The Pod Decision Service follows.

- [How a break is sold](#how-a-break-is-sold)
- [The guidance and its overlays](#the-guidance-and-its-overlays)
- [Prebid Server configuration](#prebid-server-configuration)
- [The fast-ctv module](#the-fast-ctv-module)
- [Demo demand and the bidder simulator](#demo-demand-and-the-bidder-simulator)
- [Scaling: the census gate, dayparts and warm-up](#scaling-the-census-gate-dayparts-and-warm-up)
- [Deploy and destroy](#deploy-and-destroy)
- [Rules the configuration relies on](#rules-the-configuration-relies-on)

## How a break is sold

1. At each ad marker, MediaTailor POSTs to the guidance's CloudFront distribution. The URL carries the break's
   `evid`, `upid` and `dur`, the header templates carry the session id and the origin secret, and the body names
   the stored request for the break's length.
2. A WAF rule group of the stage, referenced first by the guidance's web ACL, blocks `/openrtb2/*` without the
   origin secret.
3. Prebid Server loads the stored request: one pod of 30 s slots for the own book only.
4. The fast-ctv module's policy hook asks the Pod Decision Service which bidders, regulations and device fields this
   break may use. Only on a positive, in-time answer does it add them.
5. The auction runs (`tmax` 500 ms). The module's exitpoint hook has the Pod Decision Service select the pod, and
   renders it as one VAST 4.2 document. Every ad carries our own impression tracker.
6. MediaTailor stitches the winners in `sequence` order. The Channel Assembly slate plays any time left unfilled.

## The guidance and its overlays

The guidance is its own CDK app (ADR-0002 exception A). It is never composed into this repository's app, and never
committed: `scripts/prebid_guidance.py` fetches it into `build/prebid-guidance/` at the commit that
`vendor/prebid-guidance/PIN.toml` pins (tag `v1.4.0`, commit `0c7e7e7`).

```bash
uv run python scripts/prebid_guidance.py checkout   # the pinned commit, clean, in build/prebid-guidance/checkout
uv run python scripts/prebid_guidance.py verify     # checkout, then the pin, the pinned files and the patches hold
uv run python scripts/prebid_guidance.py apply      # checkout, verify, apply every overlay
```

`verify` and `apply` refuse the checkout when:
- the tag no longer resolves to the pinned commit;
- an upstream file that an overlay patches or relies on no longer has its pinned SHA-256;
- a patch no longer applies.

The CI job `guidance-overlays` runs `apply` and the overlay tests on every run. To move the pin, change `tag` and
`commit`, run `verify`, review each file it reports against the overlays, regenerate the patches and update the
hashes.

| Overlay | Upstream files | Change | Why |
|---|---|---|---|
| O1 | `source/infrastructure/app.py`, `prebid_server/prebid_glue_constructs.py` | CDK context `stackPrefix` (stacks `<prefix>-pbs`, `<prefix>-pbs-sim`, and the Glue database `<prefix>-prebid-server-<region>-metricsetl-database`) and a dedicated bootstrap (`bootstrapQualifier`, `fileAssetsBucketName`, `imageAssetsRepositoryName`) | The shipped app hard-codes its stack names and the default bootstrap qualifier, whose execution role is often AdministratorAccess. Its Glue database name is the one name unique per account and Region that ignores the stack, so two prefixed deployments collided |
| O2 | the web ACL custom resource, `cloudfront_waf_construct.py`, `stack_cfn_parameters.py`, `cloudfront_entry_deployment.py` | Stack parameter `ExtraWafRuleGroupArn`: the web ACL references that rule group at priority 0 | The guidance has no extension point for customer rules, and deletes its web ACL with the lock token from creation, so a later `UpdateWebACL` breaks stack deletion. Rotating the origin secret updates our rule group only |
| O4 | `source/loadtest/amt-bidder/copy-bidder-files.sh` | Ends by running `copy-fast-ctv.sh`, which copies the fast-ctv module into the Prebid Server tree | The guidance's only injection point for code (`INCLUDE_AMT_BIDDER=true`) |
| O5 | the bidder simulator's `handler.py` | `video_creatives.json` beside the handler replaces the shipped creatives; one log line per request with the header names it received | The shipped creatives (5, 15 and 30 s progressive MP4s, one advertiser domain) cannot fill a CTV pod |
| O7 | `ecs_service_construct.py` | Minimum healthy 100%, maximum 200% | A forced deployment, the way a new configuration is loaded, otherwise runs at 50% of the desired tasks |

`PIN.toml` also pins files the overlays rely on without patching them: the Dockerfile (the injection point), the
entrypoint and `bootstrap.sh` (which copy `prebid-server/current/` over `default/` file for file), the default
configuration that the rendered one repeats, and the deploy-time upload of the configuration directories.

## Prebid Server configuration

`render-config` renders the stage's configuration from its `modules.demand` and `modules.pds` sections and the CDN
that serves its content (the Media stack's `ContentBaseUrl`):

```bash
uv run python scripts/prebid_guidance.py render-config --stage fastm   # into build/prebid-guidance/fastm-config/
```

The files follow the guidance's `deployment/ecr/prebid-server/` layout, from which the guidance uploads them at
deploy time. Golden copies for a sample stage are in `core/tests/__snapshots__/test_pbs_config/`.

| File | Content |
|---|---|
| `current-config/prebid-config.yaml` | Replaces the guidance's default file, so it repeats every default it keeps. Changes: `server.http.server-instances` = `modules.demand.server_instances`; the five shipped adapters (`appnexus`, `ix`, `openx`, `pubmatic`, `rubicon`) off; the simulator (`amt`) on in `demo` mode only; the own book `ownbook`, a `generic` alias on `http://pds.<prefix>.fast.internal/ownbook/openrtb` (OpenRTB 2.6); `hooks.fast-ctv` (policy and select URLs and timeouts, the tracker template, request logging); GDPR out of scope unless a request signals it; the unprotected log-level endpoint off |
| `stored_requests/fast-pod-{30,60,90,120}s.json` | One pod (`podid` 1) of one to four 30 s slots (`poddur` = the break, `rqddurs` [30], `slotinpod` 1 first, -1 last); `app.content` `livestream` 1, `context` 1, `cattax` 9; a complete `source.schain`; `regs.coppa` 0; `imp.ssai` 3; `tmax` 500; bidder: the own book only; no `device` or `user` |
| `stored_requests/fast-pod-kids-{…}s.json` | The same with `regs.coppa` 1, for child-directed playback configurations |
| `stored_accounts/<prefix>.json` | The execution plan: `processed-auction-request` → `fast-ctv-policy` inside `modules.pds.policy_group_timeout_ms`, `exitpoint` → `fast-ctv-vast` inside `select_group_timeout_ms`. Activity Controls deny `transmitEids`, `transmitUfpd` and `transmitPreciseGeo` to every bidder but the own book. Debug output is off |

The Pod Decision Service is named `pds.<prefix>.fast.internal`, in the stage's private hosted zone in the Prebid
Server VPC. The configuration can therefore be rendered, and deployed, before the service exists. Until then the
policy hook falls back and every pod is the own book's, or empty. Nothing in the configuration is secret: the origin
secret lives only in the WAF rule group and the playback configurations.

## The fast-ctv module

`pbs/fast-ctv` is a Prebid Server Java module with two hooks. Each account's execution plan decides which hooks run,
inside which group timeouts. Overlay O4 compiles it into the guidance's image. It is the only Java in the repository
(ADR-0001 as amended).

| Hook (stage) | What it does | On any failure |
|---|---|---|
| `fast-ctv-policy` (processed-auction-request; HTTP timeout `policy-timeout-ms`, group `policy_group_timeout_ms`) | POSTs the break to `/policy`: the ticket (only its `[A-Za-z0-9_-]` characters, at most 22), the session, the playback configuration and its Function release, `evid`, `upid`, `dur_ms`, the user agent, `X-Forwarded-For`, `CloudFront-Viewer-Address` and the content context (at most 1,000 characters). On HTTP 200 with an answer the contract accepts for this break's `decision_id`, it applies the patch: it adds the bidders to every imp beside the own book (never in `SAFE` or `KIDS` breaks, whatever the answer says), and sets `regs`, `device` (a null `ip` or `ipv6` removes the address Prebid Server took from the client-settable `X-Forwarded-For`), `app.content`, `user` (`FULL` only), first-party data scoping and deals | No change: a break without a valid session, configuration, event id or duration is not asked about (`POLICY_SKIPPED`); non-200, an answer the contract refuses, another break's `decision_id`, timeout, unreachable. Prebid Server's group timeout covers a hung hook the same way |
| `fast-ctv-vast` (exitpoint; HTTP timeout `select-timeout-ms`, group `select_group_timeout_ms`) | Sends every bid to `/select` (one whose `adm` is not usable VAST with `adm_kind` null, so it is recorded but never placed), then renders exactly the pod it returns as one VAST 4.2 document (`application/xml`): winners with `sequence` 1..n, then one standalone per winner for ad buffet unless `X-Fast-Buffet: 0`. Every ad gets `burl`/`nurl` as `<Impression>` (with `${AUCTION_PRICE}` replaced) and **our impression tracker**: the ones `/select` lists for it, or the module's own (`tracker-template`, `enr=0`) where it lists none, because MediaTailor drops an inline ad that has no impression and the tracker is how delivery is counted | `/select` failing or not askable: `SELECT_FALLBACK`, the auction's house ads (`ext.fast.house`) that fit `poddur`, never repeating a `crid` or advertiser domain, each with the module's own tracker. Any exception: an empty `<VAST version="4.2"/>`, so the slate plays. Hook absent or timed out: Prebid Server's JSON, which MediaTailor refuses, so the slate plays |

A bid is usable only when its `adm` is a VAST document whose `<Ad>` holds an `<InLine>` or `<Wrapper>`; a document
type declaration is refused, so there are no XML external entities. No pod rule runs in Java: eligibility,
separation, durations and value are the Pod Decision Service's (`/select`).

**The two calls** are the Pod Decision Service contract, version 1
([docs/pds.md](pds.md#contract): `contracts/pds-policy.schema.json`, `contracts/pds-select.schema.json`). Both sides
refuse unknown fields. The module's JUnit tests read every example in `contracts/fixtures/pds/{policy,select}/`: the
module must ask exactly each example's request (from the same headers and query) and render each example's response.
The contract tests validate every call the real jar makes against the contract's models.

- `/policy` request: `{v, t, sid, pc, fv, evid, upid, dur_ms, ua, xff, cfva, ctx, probe}`. Response: `{v,
  decision_id, mode, reason, bidders, regs: {coppa, gpp, gpp_sid}, device: {ip, ipv6, devicetype}, app_content,
  user, fpd: {bidders}, deals: [{id, bidfloor, at, wseat}]}`.
- `/select` request: `{v, decision_id, sid, pc, evid, dur_ms, mode, probe, slots: [{impid, poddur, rqddurs}], bids:
  [{ref, bidder, seat, impid, price, dur, vast_dur_ms, crid, adomain, cat, cattax, dealid, house, has_impression,
  adm_kind}]}`. `decision_id` is `<sid>.<evid>`; `mode` is null when no patch was applied; `bidder` is the request's
  bidder code (`ownbook`), which Prebid Server's seat carries (its `adaptercode` names the adapter behind an alias,
  `generic`). Bids the schema cannot carry (an invalid bidder name, no price) are left out. Response: `{v, pod:
  [{impid, ref, sequence}], standalone: [{impid, ref}], trackers: {ref: [url]}, reason}`. A response that names an
  unknown or unusable bid, places a bid in another slot or repeats a sequence is refused.

**Logs.** With `log-requests: true` the module writes two JSON lines per auction to the Prebid Server log:
`fast-ctv-request` (header *names*, the ticket's length, the session, the break) and `fast-ctv-pod` (the policy
outcome and latency, the selection or the fallback reason, the winners and standalones as `seat:crid`). Neither
ever holds the ticket or the origin secret.

**Build and tests** (Java 21; CI job `pbs-module`):

```bash
make pbs-test       # Prebid Server Java at the pinned commit + the guidance's modules + ours, as the image builds it; JUnit
make pbs-contract   # the contract tests against that jar, with local mocks of every endpoint it calls
```

`scripts/pbs_module.py build` repeats the guidance Dockerfile's build stage natively. It refuses a Prebid Server Java
tag that does not resolve to the commit in `PIN.toml`, and also writes `prebid-server-stock.jar`, the same jar
without the module. The contract tests (`tests/pbs`) run the jar with the stage's rendered configuration:

| Contract | Checked |
|---|---|
| No-hook | Third-party bidders are called only after a positive, in-time `/policy` answer. Zero calls for: the module disabled host-wide; the stock jar; `/policy` unreachable, slower than the module's timeout, past the account's group timeout, failing, malformed or of another schema version; module execution off for the account; hook ids absent from the plan. Every privacy mode is exercised |
| Header isolation | No `X-Fast-*`, `User-Agent` or `X-Forwarded-For` header reaches any bidder |
| Wire fields | `podid`, `slotinpod`, `poddur`, `rqddurs` and `imp.ssai` reach OpenRTB 2.5 and 2.6 bidders intact; the own book gets OpenRTB 2.6; content metadata reaches bidders, the 2.6 fields in `app.content.ext` on 3.43.0; precise geolocation reaches only the own book |
| Impressions | Every rendered ad has an impression, and ours, even a third-party ad with none of its own: the service's tracker, or the module's own on the house fallback |
| PDS contract | Every `/policy` and `/select` call validates against the contract's strict models, and `/select` is answered by the Pod Decision Service's own pod rules; a break without an event id asks neither |
| Durations | Every break length on the grid has a pod; 31 s and 300 s are refused with 400 |

## Demo demand and the bidder simulator

In `modules.demand.mode: demo` the stage carries simulated demand, labelled as such everywhere it shows: ten demo
creatives, a bidder simulator that bids them, and the own book's synthetic direct lines (`modules.pds.demo_lines`,
served by the Pod Decision Service).

**The demo creatives** (`fast_channels.core.demo_demand`) are part of the ladder kit. With demo mode on, every kit
also encodes one 30.03 s ad (`DEMO_AD_GOPS` = 15 GOPs, one 30 s slot of the stored requests) per brand still in
`assets/demo` (`scripts/render_brand_assets.py --demo`), through the same ingest pipeline as the house ads, so their
renditions are the ladder's:

| Creative | Served by | Advertiser domain | Simulated CPM |
|---|---|---|---|
| `sim1` … `sim8` | the bidder simulator (`amt`) | `brand-a.example` … `brand-h.example`; `sim3` and `sim4` share `brand-c.example`, so the pod's advertiser separation shows | $12.50–35.00 (the Phase-0 break table's) |
| `direct1`, `direct2` | the own book's synthetic direct lines | `direct1.example`, `direct2.example` | the line's |

- The stills stay CDK file assets. The MediaConvert role may read each one, object by object, and nothing is written
  to the ingest bucket, whose rule ingests any video file under any key (a synth test checks that no demand role
  can write to it).
- Turning demo mode on adds the stills' digest to the kit's fingerprint, so the kit re-provisions once, with ten more
  encodes of 30 s.
- The kit writes their index, `ads/<ladder>/demo/creatives.json`: each creative's id, advertiser, domain, price, the
  immutable creative id MediaTailor caches it by, its HLS copy under `ads/<ladder>/<creative id>/`, its duration
  and size.

**The bidder simulator** is the guidance's `handler.py` with overlay O5. The deploy script renders its
`video_creatives.json` from the index: one inline VAST 4.2 ad per `sim*` creative (no impression of its own, so the
fast-ctv module's notices and our tracker are its impressions), with a billing notice on the CDN's beacon. Without
the index (a stage without the Media stack) the simulator bids the guidance's own creatives. Two modes:

| `modules.demand.simulator` | Where it runs | Reached by |
|---|---|---|
| `guidance_stack` (default) | the guidance's simulator stack `<prefix>-pbs-sim`: its own VPC with two NAT gateways and an internal load balancer | VPC peering from the Prebid Server VPC (the stack's outputs become the guidance's parameters) |
| `lambda` | `<prefix>-demand-simulator`, a Lambda target of an internal load balancer in the Prebid Server VPC (`FastChannels-<prefix>-Demand`) | `http://sim.<prefix>.fast.internal/`, a record in the stage's private zone; the load balancer admits only the Prebid Server tasks' security group |

**`FastChannels-<prefix>-Demand`** holds what runs beside the guidance, in its VPC:

- the private zone `<prefix>.fast.internal` (where `pds.` and `sim.` are named);
- in `lambda` mode, the simulator.

It exists in the app only with the CDK context `fast-channels:demand-network`, which `deploy` passes after the
guidance is up. It is read from `/<prefix>/demand/*`, with each private subnet's zone and route table from EC2.
`make deploy` therefore leaves it alone, and `destroy` deletes it before the guidance. The simulator's code package is
uploaded to the guidance's assets bucket under its content hash (`simulator/<hash>.zip`).

**The smoke test** in demo mode also checks:

- **The simulator:** a direct 4-slot pod request returns one bid per slot, each a demo creative.
- **The demo creatives:** each multivariant on the CDN has the ladder's variants in order: codec, size, frame rate
  and the nominal `BANDWIDTH`.

## Scaling: the census gate, dayparts and warm-up

Prebid Server runs only while the stage airs. The `Demand` stack adds three parts to the guidance's ECS service and
its target tracking (CPU, memory, requests per target):

| Part | Trigger | What it does |
|---|---|---|
| **Census gate** (`<prefix>-demand-census-gate`, Step Functions with JSONata, SDK calls only) | The channel census's `Census Changed` event on the stage's bus (the ops module: the running-channel count crossed zero) | **First channel starts:** the scalable target's minimum goes to `tasks.min` (the tier minimum) and the desired count to at least that. **Last channel stops:** it waits 15 minutes, reads the census's last count (`/<prefix>/ops/running-channels`) again, and if no channel runs sets the minimum and the desired count to 0. A restart within the 15 minutes keeps Prebid Server up |
| **Dayparts** (`modules.demand.dayparts`) | Application Auto Scaling scheduled actions `<prefix>-daypart-<n>` at their `cron` (UTC) | Raise the minimum ahead of a busy daypart (fire at least 10 minutes before its first break); target tracking covers the time between them |
| **Warm-up** (`<prefix>-pbs-warmup`, CodeBuild on arm64) | Every task of the service that reaches `RUNNING` (`ECS Task State Change`): census, daypart or target-tracking scale-outs alike | Waits until the task is a healthy target of the guidance's load balancer (up to 10 minutes; a draining task is skipped), then replays MediaTailor's POST through the guidance's CloudFront with origin secret A, at `WARMUP_RATE_PER_TASK` (50) auctions/s per healthy target, so the new task gets about 50/s, for `WARMUP_SECONDS` (150 s), with vegeta 12.13.0 (download checksum-verified). Every request is a probe (`X-Fast-Probe: 1`) without a ticket: own book and house only, no partner sees it |

Undeliverable events of both rules go to the dead-letter queue `<prefix>-demand-dead-letters`, which alarms to the
stage's alert topic.

`deploy-demand` starts the gate once with the census's current count, so a stage with no channel running scales to
zero 15 minutes after its deploy. A later guidance deploy resets the minimum to `ECSTaskMinCapacity` (= `tasks.min`)
until the next census change. P0-21 measured scale-from-zero at about 3 minutes to all-healthy and the JIT warm-up at
2–3 minutes: open a daypart at least 10 minutes before its first break.

The warm-up waits for the target's health because load on a task that is not yet listening, or is still being
checked, fails the guidance's health checks (`/status` every 60 s, unhealthy after 2 failures, healthy after 5
successes) and ECS replaces the task. Scaling from zero, the load balancer has no healthy target and fails open: it
sends requests to tasks still starting, which refuse them (HTTP 502).

`scripts/prebid_guidance.py herd --rate 400 --seconds 6` replays a break herd with the warm-up project and prints
vegeta's status codes and latency percentiles. A warm stage serves 400/s for 6 s on 2 tasks.

## Deploy and destroy

```bash
make deploy STAGE=fastm            # the repository's stacks, DemandEdge and DemandBuild among them
make deploy-demand STAGE=fastm     # the rest, in order, then the smoke test (about 30 minutes)
make destroy-demand STAGE=fastm    # everything deploy-demand created, then the inventory
```

`scripts/prebid_guidance.py` runs each step on its own as well (`doctor`, `bootstrap`, `build-image`, `outputs`,
`upload-config`, `smoke`, `destroy`, `inventory`; `--stage`, `--prefix`, `--content-base-url`). `deploy` runs, in
order (DESIGN §4.3):

1. **The repository's stacks** `FastChannels-<prefix>-DemandEdge` (us-east-1: origin secrets A and B, the rule group)
   and `-DemandBuild` (the image repository `<prefix>-pbs`, the x86 build project `<prefix>-pbs-image`, the policy
   `<prefix>-guidance-cfn-exec`).
2. **A CDK bootstrap of the guidance's own** (stack `<prefix>-cdk-bootstrap`, qualifier = the prefix, bucket
   `<prefix>-cdk-assets-<account>-<region>`, repository `<prefix>-cdk-images-<account>-<region>`, the AWS managed S3
   key). Its CloudFormation execution role has `<prefix>-guidance-cfn-exec` instead of AdministratorAccess: per-service
   permissions for the guidance's resources; S3, Lambda and IAM roles only on the guidance's names (`<prefix>-pbs*`);
   managed-policy attachment only for the two Lambda execution policies the guidance uses; KMS without data access.
   The guidance never touches the account's shared `hnb659fds` bootstrap.
3. **The image**: the guidance's unmodified Dockerfile with `INCLUDE_AMT_BIDDER=true`, the overlays and the fast-ctv
   module, built by CodeBuild on x86 and tagged `<pbs version>-<context hash>` (an unchanged context reuses its image).
   The build refuses a Prebid Server Java tag that no longer resolves to the pinned commit.
4. **The guidance**, with the rendered configuration in its checkout (it uploads it at create time) and these
   parameters on the renamed stack's key (`<prefix>-pbs:<Name>`): `SpotInstanceWeight=0` (Fargate only),
   `ECSTaskMinCapacity`/`ECSTaskMaxCapacity` = `modules.demand.tasks`, `EnableLogAnalytics=false`,
   `ExtraWafRuleGroupArn` = the rule group, and the simulator: `SimulatorEndpoint=http://sim.<prefix>.fast.internal/`
   in `lambda` mode; in `guidance_stack` mode the guidance's simulator stack `<prefix>-pbs-sim` is deployed first
   (`EnableRtbFabric=false`) and its outputs become the VPC-peering parameters. Every stack is tagged `fast:app`,
   `fast:owner=iac` and `fast:module=demand`.
5. **The SSM contract** `/<prefix>/demand/`: `cloudfront-domain`, `settings-bucket`, `stored-requests-bucket`,
   `cluster-arn`, `service-arn`, `task-security-group-id`, `vpc-id`, `private-subnet-ids`, `alb-arn`, `web-acl-arn`,
   `image-uri`, `guidance`. The repository's later stacks read the guidance only through these.
6. **`FastChannels-<prefix>-Demand`**, with the guidance's network and service as context: the private zone, in
   `lambda` mode the simulator with its code package ([above](#demo-demand-and-the-bidder-simulator)), the census
   gate, the daypart actions and the warm-up ([scaling](#scaling-the-census-gate-dayparts-and-warm-up)). With
   `modules.pds.enabled`, **`FastChannels-<prefix>-Pds`** follows with the same context: the Pod Decision Service at
   `pds.<prefix>.fast.internal` ([docs/pds.md](pds.md#the-service)). Then one run of each census gate with the current
   count.
7. **The smoke test**: a synthetic MediaTailor POST with origin secret A for each grid duration must answer 200 with a
   VAST 4.2 document as `application/xml` (empty until the Pod Decision Service exists: the module logs
   `POLICY_UNAVAILABLE`); the same POST without the secret, and a request to another path, are blocked at the edge
   (403); an off-grid duration is refused (400). In demo mode the simulator and the demo creatives are checked too.

`upload-config` uploads a changed configuration to the guidance's `current/` prefix and stored-data bucket and forces
a new ECS deployment, which overlay O7 runs at full capacity.

`destroy` deletes the Pds and Demand stacks and the guidance's stacks, then what the guidance's templates retain (the
S3 buckets with every object version, the container, DataSync, Lambda and Container Insights log groups, inactive task
definitions), the SSM contract, the
bootstrap (its retained bucket and repository too) and the repository's two stacks. It records what the templates
retain (`build/prebid-guidance/<prefix>-retained.json`) before it deletes anything, so an interrupted destroy picks up
where it stopped when run again. The guidance's six KMS keys are scheduled for deletion with the 7-day minimum; they
are the only residue. The Pod Decision Service's table and config bucket are the Data stack's and stay until
`make destroy`. `inventory` lists anything else tagged `fast:module=demand` or `pds` for the prefix, or named for it,
and exits non-zero if it finds any; the tagging index's stale entries for deleted resources are not counted.

A stage with Prebid Server deployed costs about $120–150 a month while idle (two NAT gateways, the load balancers, the
web ACL, the Valkey cache minimum). Destroy throwaway stages right after use.

## Rules the configuration relies on

- **The body-template rule.** MediaTailor inserts template values raw, without JSON escaping. A `"` or `\` in a
  value, or an unresolved unquoted number, makes the body invalid, and MediaTailor then sends no request at all. The
  body therefore carries only constants and MediaTailor's own variables, each inside a JSON string. The playback
  ticket and every player parameter travel in header templates, which Prebid Server never copies to bidders.
- **The no-hook contract.** A stored request names only the own book. If the module is disabled or absent, or
  `/policy` is unreachable, slow or failing, the auction calls no third-party bidder.
- **The 30.03 s grid.** MediaTailor rounds `session.avail_duration_secs` half up and sends 300 for a missing duration.
  A break off the grid names a stored request that does not exist: Prebid Server answers 400 and the slate plays.
