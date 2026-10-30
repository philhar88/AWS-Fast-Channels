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
- [An ad server in front: Prebid results for its VAST](#an-ad-server-in-front-prebid-results-for-its-vast)
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
| `current-config/prebid-config.yaml` | Replaces the guidance's default file, so it repeats every default it keeps. Changes: `server.http.server-instances` = `modules.demand.server_instances`; the five shipped adapters (`appnexus`, `ix`, `openx`, `pubmatic`, `rubicon`) off; the simulator (`amt`) on in `demo` mode only; the own book `ownbook`, a `generic` alias on the PDS sidecar's `http://127.0.0.1:8090/ownbook/openrtb` (OpenRTB 2.6); `hooks.fast-ctv` (policy and select URLs and timeouts, the tracker template, the kit's house ads as `house-ads`, request logging); GDPR out of scope unless a request signals it; the unprotected log-level endpoint off |
| `stored_requests/fast-pod-{30,60,90,120}s.json` | One pod (`podid` 1) of one to four 30 s slots (`poddur` = the break, `rqddurs` [30], `slotinpod` 1 first, -1 last); `app.content` `livestream` 1, `context` 1, `cattax` 9; a complete `source.schain`; `regs.coppa` 0; `imp.ssai` 3; `tmax` 500; bidder: the own book only, with up to 9 bids per slot (`ext.prebid.multibid`; Prebid Server otherwise keeps one bid per bidder and slot, and the house ads would never reach the module); no `device` or `user` |
| `stored_requests/fast-pod-kids-{…}s.json` | The same with `regs.coppa` 1, for child-directed playback configurations |
| `stored_accounts/<prefix>.json` | The execution plan: `processed-auction-request` → `fast-ctv-policy` inside `modules.pds.policy_group_timeout_ms`, `exitpoint` → `fast-ctv-vast` inside `select_group_timeout_ms`. Activity Controls deny `transmitEids`, `transmitUfpd` and `transmitPreciseGeo` to every bidder but the own book. Debug output is off |

The Pod Decision Service is a sidecar in each Prebid Server task, on `http://127.0.0.1:8090`
([docs/pds.md](pds.md#the-service), ADR-0036), so the configuration names no host of its own. A stage without the PDS
has nothing on that port: the policy hook falls back and every pod is the own book's, or the kit's house ads. Nothing
in the configuration is secret: the origin secret lives only in the WAF rule group and the playback configurations.

## The fast-ctv module

`pbs/fast-ctv` is a Prebid Server Java module with two hooks. Each account's execution plan decides which hooks run,
inside which group timeouts. Overlay O4 compiles it into the guidance's image. It is the only Java in the repository
(ADR-0001 as amended).

| Hook (stage) | What it does | On any failure |
|---|---|---|
| `fast-ctv-policy` (processed-auction-request; HTTP timeout `policy-timeout-ms`, group `policy_group_timeout_ms`) | POSTs the break to `/policy`: the ticket (only its `[A-Za-z0-9_-]` characters, at most 22), the session, the playback configuration and its Function release, `evid`, `upid`, `dur_ms`, the user agent, `X-Forwarded-For`, `CloudFront-Viewer-Address` and the content context (at most 1,000 characters). On HTTP 200 with an answer the contract accepts for this break's `decision_id`, it applies the patch: it adds the bidders to every imp beside the own book (never in `SAFE` or `KIDS` breaks, whatever the answer says), and sets `regs`, `device` (a null `ip` or `ipv6` removes the address Prebid Server took from the client-settable `X-Forwarded-For`), `app.content`, `user` (`FULL` only), first-party data scoping and deals | No change: a break without a valid session, configuration, event id or duration is not asked about (`POLICY_SKIPPED`); non-200, an answer the contract refuses, another break's `decision_id`, timeout, unreachable. Prebid Server's group timeout covers a hung hook the same way |
| `fast-ctv-vast` (exitpoint; HTTP timeout `select-timeout-ms`, group `select_group_timeout_ms`) | Sends every bid to `/select` (one whose `adm` is not usable VAST with `adm_kind` null, so it is recorded but never placed), then renders exactly the pod it returns as one VAST 4.2 document (`application/xml`): winners with `sequence` 1..n, then one standalone per winner for ad buffet unless `X-Fast-Buffet: 0`. Every ad gets `burl`/`nurl` as `<Impression>` (with `${AUCTION_PRICE}` replaced) and **our impression tracker**: the ones `/select` lists for it, or the module's own (`tracker-template`, `enr=0`) where it lists none, because MediaTailor drops an inline ad that has no impression and the tracker is how delivery is counted | `/select` failing or not askable: `SELECT_FALLBACK`, the auction's house ads (`ext.fast.house`) that fit `poddur`, never repeating a `crid` or advertiser domain, each with the module's own tracker. A break served without the `/policy` answer is SAFE (own book and house only). If its pod would still be empty (the own book silent too, or `/select` unavailable and the auction without a house ad), the module renders the kit's house ads from `house-ads` (`HOUSE_CONFIG`), so the break never airs the slate for want of an answer; a pod `/select` chose empty with the policy applied stays empty. Any exception: an empty `<VAST version="4.2"/>`, so the slate plays. Hook absent or timed out: Prebid Server's JSON, which MediaTailor refuses, so the slate plays |

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
outcome and latency, `safe` when the break was served without the policy, the selection or the fallback reason, the winners and standalones as `seat:crid`). Neither
ever holds the ticket or the origin secret.

**Build and tests** (Java 21 for 3.43.0, Java 25 for 4.4.0; CI job `pbs-module`, once per release):

```bash
make pbs-test       # Prebid Server Java at the pinned commit + the guidance's modules + ours, as the image builds it; JUnit
make pbs-contract   # the contract tests against that jar, with local mocks of every endpoint it calls
make pbs-test pbs-contract PBS_VERSION=4.4.0   # the same on 4.4.0
```

When the `java` on PATH is not the release's JDK, `scripts/pbs_module.py` downloads the Corretto pinned in
`PIN.toml` (`[jdks]`), checks its SHA-256 and uses it from `build/tools`.

`scripts/pbs_module.py build` repeats the guidance Dockerfile's build stage natively. It refuses a Prebid Server Java
tag that does not resolve to the commit in `PIN.toml`, and also writes `prebid-server-stock.jar`, the same jar
without the module. The contract tests (`tests/pbs`) run the jar with the stage's rendered configuration:

| Contract | Checked |
|---|---|
| No-hook | Third-party bidders are called only after a positive, in-time `/policy` answer. Zero calls for: the module disabled host-wide; the stock jar; `/policy` unreachable, slower than the module's timeout, past the account's group timeout, failing, malformed or of another schema version; module execution off for the account; hook ids absent from the plan. Every privacy mode is exercised |
| Header isolation | No `X-Fast-*`, `User-Agent` or `X-Forwarded-For` header reaches any bidder |
| Wire fields | `podid`, `slotinpod`, `poddur`, `rqddurs` and `imp.ssai` reach OpenRTB 2.5 and 2.6 bidders intact (`podid` is a string on 4.4.0, as OpenRTB 2.6 types it, and an integer on 3.43.0); the own book gets OpenRTB 2.6; content metadata reaches bidders. The 2.6 content fields (`genres`, `gtax`, `realtime`, `firstbroadcast`, `data[].cids`) arrive first-class on 4.4.0; 3.43.0 drops them, so there they travel in `app.content.ext`. Precise geolocation and `app.content.data` reach only the own book |
| Guidance modules | The guidance's `amt` adapter (the simulator's bidder) and its log analytics reporter run on the release. The reporter logs every request header, the ticket and the origin secret included, so stages deploy with `EnableLogAnalytics=false` |
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
| **Warm-up** (`<prefix>-pbs-warmup`, CodeBuild on arm64) | Every task of the service that reaches `RUNNING` (`ECS Task State Change`): census, daypart or target-tracking scale-outs alike | Waits until the task is a healthy target of the guidance's load balancer (up to 10 minutes; a draining task is skipped), then replays MediaTailor's POST through the guidance's CloudFront with origin secret A, at `WARMUP_RATE_PER_TASK` (25) auctions/s per healthy target, so a lone new task gets about 25/s and tasks warming together about 50/s each, for `WARMUP_SECONDS` (150 s), with vegeta 12.13.0 (download checksum-verified). Every request is a probe (`X-Fast-Probe: 1`) without a ticket: own book and house only, no partner sees it |

Undeliverable events of both rules go to the dead-letter queue `<prefix>-demand-dead-letters`, which alarms to the
stage's alert topic.

With `modules.pds`, `scripts/guidance_app.py` also sets **ALB slow start** (180 s) on the guidance's Prebid Server
target groups, which route round robin (slow start does not work with least outstanding requests or weighted random):
a task that joins a serving fleet (a scale-out, a rollout) gets a linearly growing share of live requests while its
JIT and its PDS sidecar warm. Measured on a 2 to 3 scale-out: the new task took 17 of a 100-session break's auctions
(the others 42 and 41), with no fallback. Slow start does not apply to a scale from zero, whose tasks register
together with no other healthy target; the census gate's warm-up before the first break covers that.

`deploy-demand` starts the gate once with the census's current count, so a stage with no channel running scales to
zero 15 minutes after its deploy. It then warms the running tasks that no warm-up build has named and waits for them:
a first deploy starts its tasks before the warm-up rule exists. A later guidance deploy resets the minimum to `ECSTaskMinCapacity` (= `tasks.min`)
until the next census change. P0-21 measured scale-from-zero at about 3 minutes to all-healthy and the JIT warm-up at
2–3 minutes: open a daypart at least 10 minutes before its first break.

The warm-up waits for the target's health because load on a task that is not yet listening, or is still being
checked, fails the guidance's health checks (`/status` every 60 s, unhealthy after 2 failures, healthy after 5
successes) and ECS replaces the task. Scaling from zero, the load balancer has no healthy target and fails open: it
sends requests to tasks still starting, which refuse them (HTTP 502).

`scripts/prebid_guidance.py herd --rate 400 --seconds 6` replays a break herd with the warm-up project and prints
vegeta's status codes and latency percentiles. A warm stage serves 400/s for 6 s on 2 tasks.

## An ad server in front: Prebid results for its VAST

With `modules.demand.ad_server` set, an ad server decides the break and Prebid's results reach it as key-values: the
standard Prebid video flow for an ad server ([ADR 0034](adr/0034-prebid-results-for-an-ad-servers-vast.md)). The
default, `none`, is the direct route above, unchanged.

| Field | Default | Meaning |
|---|---|---|
| `modules.demand.ad_server` | `none` | `none`, `mock` (the demo's ad server), `gam` or `freewheel`. Needs the release module |
| `modules.demand.ad_server_tag` | `""` | `gam`/`freewheel`: the publisher's VAST tag, with `{prebid}` once where the key-values go |
| `modules.demand.pod_cache_seconds` | `300` | How long Prebid Cache keeps a break's pod |

How a break is sold on this route:

1. **Session start.** The PRE_SESSION tree blanks `player_params.hb_pb`, `hb_uuid` and `hb_bidder`, so a value the
   player sends never reaches the ad server.
2. **The auction.** A PRE_ADS Function POSTs the break's auction to Prebid Server through its CloudFront and the
   origin rule, before the ad request. It sends what MediaTailor's direct POST sends:
   - the break in the query;
   - the session and the configuration in headers, plus the viewer's `User-Agent`;
   - in the body, only the stored request `fast-adsrv-pod-<seconds>s`;
   - the origin secret and the release, which the Release Controller adds when it creates the Function.

   The Function is cached per session and break (scope `session_break`, 30 s), so a break's auction runs once. Its
   request timeout is 1,200 ms, and with the 2,000 ms ad request the siblings' 6,000 ms Live maximum holds.
3. **The pod.**
   - Prebid Server runs the auction as on the direct route: `/policy` patches the request, and `/select` picks the
     pod, with its reservations and pod rules.
   - The fast-ctv module renders the pod, our impression tracker on every ad. The stored request's
     `ext.fast.pod_cache` asks it to store that pod in Prebid Cache once, through Prebid Server's own cache client.
   - It answers one bid with the pod's Prebid targeting:
     - `hb_pb`: the bucket of the sum of the paid winners' prices, in CTV buckets of 0.10 up to $20, 0.50 up to $50
       and 1.00 up to $100;
     - `hb_uuid` and `hb_cache_id`: the cache key;
     - `hb_bidder`: `fast-ctv`;
     - `hb_cache_host` and `hb_cache_path`.
   - A pod of house ads only, an empty pod or a failed write answers no bid.
4. **The ad request.** MediaTailor GETs the ad server's tag with the three keys for this break. A Prebid line item
   that matches `hb_pb` answers a VAST Wrapper to `https://<guidance CloudFront>/cache?uuid=<hb_uuid>`. Anything else
   gets the ad server's own ads.
5. **The pod plays.** MediaTailor follows the Wrapper and stitches the pod as it stitches the direct route's:
   winners in `sequence` order, a standalone in a slot whose ad fails, slate for the rest.

**Prebid Cache** is the guidance's own: ElastiCache Serverless (Valkey) behind its cache function, with uuid4 keys.
- **Writes stay inside the VPC.** Prebid Server's `cache.internal` names `cache.<prefix>.fast.internal`, a private
  record of the guidance's internal load balancer, which admits only the Prebid Server tasks.
- **Reads are public.** The origin rule group opens `/cache` only to `GET` or `HEAD` with a `uuid` that is a uuid4.
  Any other method, path form or query, a public `POST` included, is blocked at the edge. CloudFront caches a read
  per `uuid` for the entry's remaining life.

The private record needs the guidance's internal load balancer, which the deploy finds in the guidance's VPC.

**The mock ad server** (`mock`, the demo) sits beside the bidder simulator:
- It answers `GET /ads` behind an API Gateway HTTP API (throttled to 200/s), and only for requests that carry the
  stage's origin secret, as the origin rule requires of Prebid Server's callers. The function's permission is scoped
  to the API.
- It works like a GAM network with one Prebid line item per price bucket. A request whose `hb_pb` is a bucket, with
  a uuid4 `hb_uuid` and a bidder, gets the Prebid line's Wrapper to the cached pod (`allowMultipleAds="true"`).
  Anything else gets the house line's Wrapper to the stage's house pod.
- It logs each decision with its line, session and break.

**Google Ad Manager** (`gam`). These instructions cannot be tested here without a GAM network. They follow Prebid's
setup guides for GAM (*Google Ad Manager with Prebid Step by Step* and *Setting up Prebid Video in GAM*):

- **Tag:** the publisher's VAST tag with the key-values inside `cust_params`, for example
  `https://pubads.g.doubleclick.net/gampad/ads?iu=/<network>/<ad unit>&sz=1920x1080&gdfp_req=1&output=vast&env=vp&unviewed_position_start=1&correlator=[avail.random]&cust_params={prebid}`.
  The deployment replaces `{prebid}` with `hb_pb%3D[player_params.hb_pb]%26hb_uuid%3D[player_params.hb_uuid]%26hb_bidder%3D[player_params.hb_bidder]`.
- **Line items:** price-priority line items, one per bucket above, each targeting `hb_pb` with its bucket.
- **Creative:** a VAST redirect creative with the URL `https://<guidance CloudFront>/cache?uuid=%%PATTERN:hb_uuid%%`,
  its duration the longest break.
- **Rule:** the pod a Wrapper leads to must be the whole break, so do not let an ad rule add other ads beside it.

**FreeWheel** (`freewheel`). These instructions cannot be tested here without a FreeWheel network. They follow
Prebid's *Setting up Prebid Video in FreeWheel*:

- **Tag:** the publisher's ad request with `{prebid}` in its key-value section, after the first `;`. The deployment
  replaces it with `hb_pb=[player_params.hb_pb]&hb_uuid=[player_params.hb_uuid]&hb_bidder=[player_params.hb_bidder]`.
- **Placements:** one per bucket, each with custom targeting `hb_pb` on its bucket.
- **Creative:** VAST URL `https://<guidance CloudFront>/cache?uuid=#{request.keyValue("hb_uuid")}`.
- **Difference from Prebid's guide:** Prebid's FreeWheel long-form guide targets `hb_pb_cat_dur`, one key per ad, so
  that FreeWheel assembles the pod. Here the pod is already assembled, under the Pod Decision Service's rules, so one
  key decides it.

**What this route trades.**
- The ad server decides the break as one unit: one price for the whole pod.
- The auction adds up to 1,300 ms before the ad request.
- The auction is MediaTailor's Function request, which carries no viewer IP, so `/policy` sets no `device.ip`.
- A break answers no bid when the cache write does not finish within the exitpoint's time. That happens when the
  cache function is cold, or when `/select` is slow and leaves no time for the write. The ad server then serves its
  house ad. In the end-to-end run this hit 2 of 9 breaks: one write timed out at 304 ms, and once `/select` took
  370 ms.

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
   and `-DemandBuild` (the image repository `<prefix>-pbs`, with `modules.pds` the sidecar's `<prefix>-pds`, the
   x86 build project `<prefix>-pbs-image`, the policy `<prefix>-guidance-cfn-exec`).
2. **A CDK bootstrap of the guidance's own** (stack `<prefix>-cdk-bootstrap`, qualifier = the prefix, bucket
   `<prefix>-cdk-assets-<account>-<region>`, repository `<prefix>-cdk-images-<account>-<region>`, the AWS managed S3
   key). Its CloudFormation execution role has `<prefix>-guidance-cfn-exec` instead of AdministratorAccess: per-service
   permissions for the guidance's resources; S3, Lambda and IAM roles only on the guidance's names (`<prefix>-pbs*`);
   managed-policy attachment only for the two Lambda execution policies the guidance uses; KMS without data access.
   The guidance never touches the account's shared `hnb659fds` bootstrap.
3. **The image**: the guidance's unmodified Dockerfile with `INCLUDE_AMT_BIDDER=true`, the overlays and the fast-ctv
   module, built by CodeBuild on x86 and tagged `<pbs version>-<context hash>` (an unchanged context reuses its image).
   The build refuses a Prebid Server Java tag that no longer resolves to the pinned commit. With `modules.pds`, the
   same project builds the PDS sidecar's image (`pds/Dockerfile`, tagged by its context hash) into `<prefix>-pds`.
4. **The guidance**, with the rendered configuration in its checkout (it uploads it at create time) and these
   parameters on the renamed stack's key (`<prefix>-pbs:<Name>`): `SpotInstanceWeight=0` (Fargate only),
   `ECSTaskMinCapacity`/`ECSTaskMaxCapacity` = `modules.demand.tasks`, `EnableLogAnalytics=false`,
   `ExtraWafRuleGroupArn` = the rule group, and the simulator: `SimulatorEndpoint=http://sim.<prefix>.fast.internal/`
   in `lambda` mode; in `guidance_stack` mode the guidance's simulator stack `<prefix>-pbs-sim` is deployed first
   (`EnableRtbFabric=false`) and its outputs become the VPC-peering parameters. Every stack is tagged `fast:app`,
   `fast:owner=iac` and `fast:module=demand`. With `modules.pds`, the guidance's app is deployed through
   `scripts/guidance_app.py`, which adds the PDS sidecar to its task: the container, 6 GiB of task memory, the
   sidecar's grants and its log group ([docs/pds.md](pds.md#how-the-sidecar-joins-the-guidances-task)). It needs
   the Data and Media stacks' `/<prefix>/pds/*` parameters (`make deploy` first).
5. **The SSM contract** `/<prefix>/demand/`: `cloudfront-domain`, `settings-bucket`, `stored-requests-bucket`,
   `cluster-arn`, `service-arn`, `task-security-group-id`, `vpc-id`, `private-subnet-ids`, `alb-arn`, `web-acl-arn`,
   `image-uri`, `guidance`. The repository's later stacks read the guidance only through these.
6. **The configuration**: the rendered files are compared with what the guidance serves (`current/` in its settings
   bucket, the stored data in its stored-requests bucket), what differs is uploaded, everything is read back and must
   match, and if anything changed Prebid Server's tasks roll (overlay O7 keeps full capacity). The guidance uploads
   its configuration directory only when its stack is created, so without this a changed setting (a timeout, the house
   ads) would never reach an existing stage.
7. **`FastChannels-<prefix>-Demand`**, with the guidance's network and service as context: the private zone, in
   `lambda` mode the simulator with its code package ([above](#demo-demand-and-the-bidder-simulator)), the census
   gate, the daypart actions and the warm-up ([scaling](#scaling-the-census-gate-dayparts-and-warm-up)). With
   `modules.pds.enabled`, **`FastChannels-<prefix>-Pds`** follows with the same context: the PDS beside its sidecar
   (the publisher, delivery, the sweeper, the fallback alarms and the decision records' streams,
   [docs/pds.md](pds.md#the-service)). With `modules.release.enabled`,
   **`FastChannels-<prefix>-Release`** follows: the Release Controller, whose bundle names the guidance's CloudFront
   domain ([docs/release-controller.md](release-controller.md#deploy-and-destroy)). Then one run of the census
   gate with the current count.
8. **The smoke test**: a synthetic MediaTailor POST with origin secret A for each grid duration must answer 200 with a
   VAST 4.2 document as `application/xml` (empty until the Pod Decision Service exists: the module logs
   `POLICY_UNAVAILABLE`); the same POST without the secret, and a request to another path, are blocked at the edge
   (403); an off-grid duration is refused (400). In demo mode the simulator and the demo creatives are checked too.

`upload-config` runs step 6 alone: it uploads what changed in the rendered configuration, verifies every object in
S3, and rolls the tasks only if something changed, which overlay O7 runs at full capacity.

`destroy` deletes the Release, Pds and Demand stacks and the guidance's stacks, then what the guidance's templates retain (the
S3 buckets with every object version, the container, DataSync, Lambda and Container Insights log groups, inactive task
definitions), the SSM contract, the
bootstrap (its retained bucket and repository too) and the repository's two stacks. It records what the templates
retain (`build/prebid-guidance/<prefix>-retained.json`) before it deletes anything, so an interrupted destroy picks up
where it stopped when run again. The guidance's six KMS keys are scheduled for deletion with the 7-day minimum; they
are the only residue. The Pod Decision Service's table and config bucket are the Data stack's and stay until
`make destroy`. `inventory` lists anything else tagged `fast:module=demand` or `pds` for the prefix, or named for it,
and exits non-zero if it finds any; the tagging index's stale entries for deleted resources are not counted.

**The demand leg** (BUILD-PLAN §12.3 `e2e-demand`; the `End-to-end` workflow with `leg: demand`, or the same
steps locally) deploys `STAGE=fastci MODULES=ops,demand` (the `fastci` stage with only those modules on), runs
`make deploy-demand`, then `tests/e2e/test_11_demand_edge.py`. It then destroys the demand module, the stage and the
ledger archive, and fails on any residue but KMS keys pending deletion. A run takes about two and a half hours and
costs a few US dollars. The tests take a few minutes and must start within the census gate's 15 minutes: the leg runs
no channel, so the gate then scales Prebid Server to zero.

**The ad-server leg** (`tests/e2e/test_15_ad_server.py`) needs the stage `fastciads`: `fastci` with
`ad_server: mock` and a 900 s pod cache, so that every pod of the run can still be read back once the ADS log arrives.
Deploy it with every module on, run `make deploy-demand` with the same stage, then the test. Viewers play the stable
sibling through three breaks. The test checks:

- one auction per session and break;
- the mock's Prebid line for each cached pod;
- that the stitched ads are the cached pod's;
- the house line when no bid clears;
- that the edge lets only `GET` and `HEAD /cache?uuid=<uuid4>` through.

```bash
make deploy STAGE=fastciads PREFIX=<throwaway>
make deploy-demand STAGE=fastciads PREFIX=<throwaway>
uv run pytest tests/e2e/test_15_ad_server.py -m e2e --prefix <throwaway> -v
make destroy-demand STAGE=fastciads PREFIX=<throwaway> && make destroy STAGE=fastciads PREFIX=<throwaway>
```

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
