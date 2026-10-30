# 0036 — The Pod Decision Service as a sidecar in the Prebid Server task

**Status:** Proposed. Supersedes [0020](0020-demand-through-the-prebid-server-guidance.md)'s placement of the Pod
Decision Service (Lambda functions behind an internal load balancer, kept warm by census-gated provisioned
concurrency) and amends 0020's deployment of the guidance: with `modules.pds` on, the guidance's own app is deployed
through a thin wrapper that adds the sidecar. It is accepted when the MediaTailor-driven demand leg
(`tests/e2e/test_14_demand_pods.py`) passes on it.

## Context

The fast-ctv module asks the Pod Decision Service (PDS) twice per break: `/policy` before the auction and `/select`
after it. The own book is a third call, an OpenRTB bidder Prebid Server knows as `ownbook`. Each call has a budget
inside MediaTailor's wait for the ad response; the design's budgets were 50 ms for `/policy` and 150 ms for
`/select`.

0020 put the three endpoints in three Lambda functions behind an internal Application Load Balancer in the Prebid
Server VPC. On a throwaway stage (M2-x2, October 2026), that path measured:

- **The load balancer's invoke:** 20–40 ms of `target_processing_time` against 3–9 ms in the handler. A module
  timeout showed up as the load balancer's HTTP 460.
- **Cold starts:** 2.1–2.6 s of init without bytecode in the bundle, 0.7–1.7 s with deterministic bytecode.
- **The break herd:** a 100-session herd needed up to 8 `/policy`, 17 `/select` and 9 `/ownbook/openrtb` environments
  at once, about 0.34 provisioned environments per session. Below that, spilled requests waited for cold starts,
  which cascaded. Provisioned concurrency of 20/30/20 costs about $1.45 an hour while channels run.
- **Idle connections:** DynamoDB drops a connection after 90 s or more idle. The next reservation reopens it (TLS),
  taking up to about 110 ms.

So the budgets became 80 ms (`/policy`) and 200 ms (`/select`). Prefetch
([0033](0033-prefetch-single-schedules.md)) spreads the herd for the sessions present in the retrieval window: the
peak arrival rate is 14–17× lower (median about 16×), about 0.020–0.024 environments per session. Late joiners, and
breaks too close to prefetch, still herd at the marker.

The owner decided on 2026-10-30 to run the PDS hot path as a second container in each Prebid Server task, reached on
localhost and scaled with Prebid Server: "extend pbs task neatly by referencing that guidance when building and just
augmenting. dont fork the whole pbs guidance for this".

## Decision

- **Placement.** `/policy`, `/select` and `/ownbook/openrtb` run in a container `Pds-Container` of each of the
  guidance's Prebid Server task definitions, on `127.0.0.1:8090`. The rendered configuration points `policy-url`,
  `select-url` and the own book's `generic` endpoint there (`core.pbs_config.PDS_BASE_URL`). Prebid Server's container
  depends on the sidecar being `HEALTHY`, so it starts only once the sidecar answers. The sidecar scales with Prebid
  Server: the Demand stack's census gate and the guidance's autoscaling.
  - Delivery, the reservations' sweeper and the snapshot publisher stay Lambda functions in the Pds stack.
  - The internal load balancer, its target groups and record, the three functions with their aliases and the
    provisioned-concurrency gate are gone. Prebid Server's task scaling stays.
- **The server: uvicorn with uvloop and httptools, one process, a plain ASGI app** (`fast_channels.pds.server`). It
  is small, production-grade and serves the same pure `core.pds` code the functions ran:
  - **Fast, deterministic startup.** The image holds precompiled bytecode. The ASGI lifespan loads the snapshot, the
    lines' budgets and, with the crawl module, the catalog contexts, and opens the reservations' DynamoDB connection,
    all before uvicorn listens. A load that fails leaves the empty value, which its background refresh replaces.
    `/health` answers 200 once the server listens, so a first deploy cannot deadlock: the snapshot is published after
    the guidance is up. Without a snapshot, `/policy` and `/select` answer 503 at once (tested under 20 ms), so the
    module serves the break SAFE and, with the own book silent, renders the kit's house ads from its configuration
    (`HOUSE_CONFIG`).
  - **DynamoDB and the pod rules off the event loop.** The endpoints are the functions' synchronous code. Each request
    runs on a bounded pool of 16 threads (`state.WORKERS`) with the workspace's locked boto3, whose client is
    thread-safe and gets as many pooled connections. The loop only parses HTTP and hands requests over. There is no
    async SDK: aiobotocore pins narrow botocore ranges, which would lock the workspace's boto3 upgrades and break
    dependency bumps. The pool's queue wait and the loop's lag are measured (below); a pool that queues would be the
    signal to revisit.
  - **Logging and metrics as before.** Powertools writes the same JSON lines to stdout. The awslogs driver delivers
    them to the sidecar's log group `/aws/ecs/<prefix>-pds`, where the lake's subscriptions match `record` as they did
    on the functions' groups (both of the group's two filter slots). Powertools `Metrics` is not thread-safe, so the
    endpoints count into a locked counter that the loop emits as EMF every minute, in the same `FastChannels`
    namespace. Each minute the sidecar also logs a `pds stats` line: per-endpoint latency, pool queue wait, loop lag,
    CPU share and peak memory.
  - **Keep-alive 120 s,** longer than Prebid Server's Vert.x client keeps an idle connection (60 s), so the client
    always closes first. uvicorn's default, 5 s, would race requests on closing connections.
- **The image** (`pds/Dockerfile`) copies the `fast-channels-pds` package and its locked dependencies onto a
  digest-pinned `python:3.14-slim`. `scripts/_demand.py` installs them for x86 with deterministic bytecode
  (`infra.bundling`); no build step runs Python. It runs as uid 1000 on a read-only root filesystem, with an in-place
  restart policy. The stage's x86 CodeBuild project builds it into the repository `<prefix>-pds`, tagged by the
  content of its build context.
- **Augmenting the guidance without patching it (amends 0020).** With `modules.pds` on, `deploy-demand` runs
  `scripts/guidance_app.py` instead of the guidance's `app.py`. It imports the checkout's `app` module, puts a
  subclass of the guidance's own `PrebidServerStack` where `build_app` looks for the stack class, and calls
  `build_app` unchanged. The subclass builds the guidance's stack, then adds to it with escape hatches:
  - the sidecar's container on each `PrebidTaskDef` (one per entry deployment, of which CloudFormation's conditions
    deploy one), and Prebid Server's dependency on it;
  - the task's CPU and memory, as overrides of the L1 task definition;
  - the sidecar's grants on the task role;
  - a DynamoDB gateway endpoint in the guidance's VPC, whose policy admits only item calls on the PDS table and the
    ledger (the guidance calls no DynamoDB), so reservations do not go out through a NAT gateway;
  - ALB slow start on the guidance's Prebid Server target groups (below);
  - the sidecar's S3 reads (the snapshot object; with the crawl module the catalog-context objects) in the policy of
    the guidance VPC's S3 gateway endpoint, for the account's resources only. That endpoint lets requests through only
    to the guidance's own buckets, so S3 refused the snapshot read (`AccessDenied`) whatever the task role allowed,
    and every break fell back until the reads joined it (seen on the first deploy);
  - the sidecar's log group, at the stack's root, so no condition applies to it.

  The sidecar's image, environment, grants and sizes reach the wrapper as CDK context, rendered by
  `core.pds.sidecar`. The wrapper runs in the guidance's own Python and CDK, without this repository's packages. It
  augments right after construction, not from an Aspect: an Aspect that adds constructs during synthesis is
  discouraged by CDK, and its constructs might escape the guidance's own `ConditionAspect`. `PIN.toml [relied_on]`
  hashes the upstream files the wrapper relies on (`prebid_server_stack.py`, `ecs_task_construct.py`,
  `alb_entry_deployment.py`, `stack_constants.py`, `vpc_construct.py`; `app.py`, `cloudfront_entry_deployment.py` and
  `ecs_service_construct.py` are pinned as patched), so an upstream change fails `verify` before it reaches a deploy.
  The wrapper finds every construct it relies on before it adds anything, and a CI test synthesizes the pinned
  guidance through it.
- **The task: 2 vCPU and 6 GiB,** the guidance's 2 vCPU shared by both containers with equal weights, and 2 GiB
  more memory. Prebid Server's JVM sizes its heap from the task's memory (a quarter by default), and the sidecar needs
  about 100 MB. Weights favouring Prebid Server starved the sidecar while a new task warmed (below).
- **Overload sheds, it never queues past the caller.** A request whose wait for a worker outlasted its caller's
  timeout (the module's `/policy` and `/select` timeouts, the auction's `tmax` for the own book) is answered 503 at
  once and counted (`Shed`): the module has already fallen back for that break, so computing it would only lengthen
  the queue.
- **New tasks:** ALB slow start (180 s) on the guidance's Prebid Server target groups ramps live traffic onto a task
  that joins a serving fleet. It needs the guidance's round-robin routing (it does not work with least outstanding
  requests or weighted random; the wrapper refuses those) and does not apply to a scale from zero, whose tasks
  register together: the census gate's warm-up before the first break covers that. The warm-up's replay is 25
  auctions/s per healthy target (it was 50), so it warms the JIT without crowding the sidecar.
- **The shared task role is the trade-off.** A Fargate task has one task role, so Prebid Server's container gets the
  sidecar's grants. Those are exactly what the hot-path functions had:
  - `s3:GetObject` on the snapshot object;
  - `dynamodb:BatchGetItem` on the PDS table's `BUDGET#` items;
  - `UpdateItem` on its `BUDGET#` and `CAP#` items, and `PutItem` on its `RES#` items;
  - `PutItem` on the ledger's `pds/*` chains;
  - with the crawl module, listing and reading the catalog-context objects.

  Prebid Server runs only the pinned image with our module, and its bidders are configuration, not code. A separate
  service with its own role would bring back the network hop this decision removes. The ledger's hash chain still
  records every reservation, whoever wrote it.
- **Lines under 16 KB.** Fargate's awslogs driver splits a longer log line into several events, and a decision record
  split that way never reaches the lake. The largest record measured is below.
- **A sub-second herd is a degraded mode, not a sizing target.** A natural real-time break spreads over about one
  manifest poll (3-6 s), and prefetch spreads it further. When MediaTailor asks for every session at the marker at once
  (it could not use a retrieval, or a break had no schedule), about 50 auctions a task in half a second outrun the
  task's 2 vCPU, and the module serves the breaks it could not decide SAFE: own book and house ads, never an empty pod
  (measured below). The alarm `<prefix>-pds-burst-real-time` names that cause: the policy-fallback alarm together with
  a prefetched sibling whose ad requests were mostly real time ([release-controller.md](../release-controller.md#alarms)).
  Tasks are sized for the natural spread ([demand.md](../demand.md#sizing-prebid-server-for-a-break)).
- **Follow-up, not done here:** O1 (the stack prefix and bootstrap context) and O7 (the deployment percentages) could
  become augmentations of the same wrapper, which would retire two patches.

## Evidence

A throwaway stage (October 2026): Prebid Server 4.4.0, 2 tasks, demo demand with two direct lines, MediaTailor
sessions on a monetized channel polling every 6 s (`herd.py`), a 120.12 s break each. Every herd at the shipped
budgets (50 and 200 ms; 80 and 200 ms before them): 0 policy fallbacks, 0 select fallbacks, 0 empty pods.

| Herd | Auctions | Module `policy_ms` p50/p95/p99/max | Module `select_ms` p50/p95/p99/max | Reservation p50 / p99 | Arrival spread |
|---|---|---|---|---|---|
| 100 sessions, real time, DynamoDB over NAT | 100 | 1/4/12/12 | 29/48/74/74 | 26 / 62-72 ms | 6.7 s |
| 400 sessions, real time, over NAT | 373 | 1/7/14/29 | 46/73/120/131 | 43-46 / 115-127 ms | 7.7 s |
| 100 sessions, real time, DynamoDB gateway endpoint | 100 | 1/4/6/6 | 26/56/83/83 | 23 / 58-81 ms | 6.1 s |
| 400 sessions, real time, endpoint | 400 | 1/8/12/27 | 26/54/96/111 | 23-24 / 67-100 ms | 6.9 s |
| 400 sessions, prefetched with shaping, endpoint | 400 | 1/2/3/5 | 24/32/79/166 | 22-24 / 49-105 ms | 83.4 s |
| 400 sessions, real time, endpoint, budgets 50/150 | 400 (1 select fallback) | 1/6/18/29 | 26/73/134/150 | 24 / 125 ms | 7.8 s |

- **The Lambda path, for comparison** (M2-x2, 100 sessions): 2-15 select fallbacks per 400-500 auctions until
  provisioned concurrency reached 20/30/20 (about 0.34 environments per session); 20-40 ms of the load balancer's
  invoke on every call. The sidecar has no per-request cold start and needs no provisioned concurrency.
- **The sidecar itself, in the herds:**
  - `/policy` p99 under 5 ms in the sidecar;
  - the pool's queue wait max 3-10 ms, the event loop's lag p99 1-3 ms (max 21 ms);
  - 1-3% of a vCPU over a herd minute; resident memory 92-106 MB;
  - Prebid Server's container 530-790 MB beside it.
- **Startup:** the loads take 0.33-0.61 s before uvicorn listens. The first DynamoDB call (DNS, TCP, TLS, over NAT)
  takes 38-42 ms, the next 4-5 ms; the loop's refresh tick keeps that connection open between breaks (budget reads
  at p50 5 ms, 12 a minute).
- **DynamoDB:** the guidance's VPC had no DynamoDB endpoint, so reservations went out through a NAT gateway. A
  gateway endpoint (the wrapper adds it; its policy admits only item calls on the PDS table and the ledger) halved
  the 400-session reservation p50 (43-46 to 23-24 ms) and cut its p99 by a fifth to a half. The remaining 22-24 ms
  p50 is the 5-item transaction's own cost: it stays the same at 35 requests a minute. The 4 budget shards add
  contention at the tail of a real-time herd: p95 53 ms against 32 ms when prefetch spreads the same 400 requests,
  and concurrent transactions on a shard refused 11-48 line ads in a real-time 400-session break even after their
  retry (`LineAdsRefused`, reason `conflict`), each replaced by another bid; none in the prefetched break. **4
  shards stay**; revisit past about 400 real-time sessions per configuration per break.
- **Prefetch** spread the requests of a shaped break over 83 s instead of 7 s. The prefetcher, which runs every 5
  minutes and on `Schedule Published`, can only shape breaks at least its retrieval lead away when it runs: in a
  herd published 80 s ahead, the first two breaks stayed real time and the third was shaped.
- **A new task's warm-up shares 2 vCPU with Prebid Server's JIT** (900-1,600 CPU units for 2-3 minutes). The sidecar
  costs about 1.1 ms of CPU per request, about 0.5 vCPU at the old warm-up's rate when tasks warm together (100-140
  auctions/s each):

  | Warm-up | Sidecar pool wait p99 (max) | Shed |
  |---|---|---|
  | 50/s per healthy target, equal CPU weights | 40-153 ms (274 ms) | 400 in a minute |
  | 50/s, weights 512 (sidecar) and 1,536 (Prebid Server) | 657-2,078 ms (2.2 s) without shedding; 310 ms (501 ms) with it | 1,500 in a minute |
  | **25/s, equal weights, shedding** | **0.8-3.7 ms (19 ms)** | **0** |

  So the warm-up's rate is 25 auctions/s per healthy target, still for 150 s (the census gate's budget before a
  first break is unchanged), and the containers share the task's CPU with equal weights.
- **Slow start**, on a 2 to 3 scale-out with a 100-session break inside the new task's 180 s: the new task took 17 of
  the break's auctions and the others 42 and 41, with no fallback and no shed; its pool wait stayed under 7 ms.
- **The largest decision record** was 4.6 KB (`pds.select`, four slots, 20 bids), well under the 16 KB at which
  Fargate's log driver splits a line.

### Sub-second herds

A herd on another throwaway stage (2 tasks, the pre-#82 prefetcher) had one break whose retrievals MediaTailor did
not use: its 100 sessions asked in real time within 0.44 s (52 in one 100 ms slot), and 65 of the 100 auctions got
`POLICY_UNAVAILABLE`. The same sessions' earlier real-time break, spread over 3.06 s, had none. Joining the module's
lines with the sidecar's records by session split the time:

- for the fallbacks that reached it, the sidecar's `/policy` handler took p50 4.9 ms (0.8 ms normally); for the
  auctions that got the patch, the module saw p50 23 ms more than the handler;
- in the sidecar, the pool's queue wait reached p50 31 ms and p99 85 ms, and the event loop lagged up to 62 ms (1-2
  ms normally); the own book's handler, pure CPU, took p50 60 ms (0.6 ms); reservations p50 186 ms (21 ms);
- 7 requests were shed, and 17 more fallbacks never reached the handler in time;
- the containers' CPU per minute stayed low: the burst lasts half a second.

Replays reproduced it: synthetic MediaTailor POSTs through the guidance's CloudFront (distinct sessions,
no probe header, the 120 s stored request), 2 warm tasks unless noted:

| Arrivals | Rate per task | One process: policy / select fallbacks, module `policy_ms` max | Two processes |
|---|---|---|---|
| 100 in 0.49 s, shaped like the break above | about 100/s | 71 / 31, 163 ms | 64 / 29, 176 ms |
| 100 in 0.98 s | about 50/s | 16 / 5, 77 ms | 37 / 35, 106 ms |
| 100 in 1.95 s | about 25/s | 3 / 0, 71 ms | 0 / 0, 25 ms |
| 200 evenly over 4 s | 25/s | 0 / 0, 20 ms | 0 / 0, 16 ms |
| 400 evenly over 4 s (single process twice) | 50/s | 0 / 0, 23 and 17 ms | 1 / 2, 58 ms |
| 100 in 0.49 s on **4** tasks | 25 per task in 0.5 s | | 7 / 15, 94 ms |

- **Capacity:** about 25 auctions/s per task at 0 fallbacks; the first fallbacks near 50/s per task. Every auction
  in every run returned a pod (a module line with winners): the fallbacks were SAFE pods of the own book's and house
  ads, and the select fallbacks the auction's house ads.
- **`/select` in a burst** is bound by the reservations' contention: 100 transactions on the same lines' 4 budget
  shards within half a second. More tasks do not lift it (15 select fallbacks on 4 tasks).
- **Two server processes per task** (one per vCPU, each with its own snapshot, refresh and pool, `/policy` and the
  own book on a pool of their own) did not help: the limit is the task's CPU, shared with Prebid Server's two event
  loops, not the interpreter lock alone. With a process per vCPU the burst's handlers still ran 7-14× slower, and the
  part of the time outside the sidecar grew (p50 28 ms). At the natural spread one process measured equal or better
  (handler p99 about 4 ms against 14 ms), so the sidecar stays one process.
- **Free-threaded Python (3.14t)**, the other way past the lock, was considered and not tried: the C extensions the
  sidecar serves with (uvloop, httptools, pydantic-core) would each need free-threaded builds and a thread-safety
  review, and the two-process run shows the CPU, which free threading does not add, is the limit.
- **More CPU per task** (4 vCPU) or more tasks per break would absorb the burst, at their cost for every break; not
  done, since the burst comes from a failure path whose known trigger (an unconsumed retrieval) M3-06's #82 removed.
- **One HTTP 500** in the first burst on a warm task was Prebid Server's metrics race (a
  `ConcurrentModificationException` in `AdapterTypeMetrics.requestType`, a `HashMap.computeIfAbsent` from
  `Metrics.updateAuctionTcfAndLmtMetrics`): the warm-up's probe auctions run with no bidder (`/policy` answers SAFE
  with `bidders: []` to a probe), so the first real auctions create the simulator adapter's metric keys on both event
  loops at once. Follow-up, deferred: a warm-up that reaches every configured adapter through stored bid responses
  (Prebid Server answers an adapter from the stored body only when it builds exactly one request, and calls the real
  bidder otherwise, so each adapter's single request must be proved first) would create those keys and warm the
  adapters' code, without a bid request to any partner.

### Reservation holds

A line ad reserved at `/select` is released two hours later if no impression arrived (`RELEASE_AFTER`; the sweeper
runs every 15 minutes). The bursts above (about 2,300 auctions in an hour) held a $1 demo line's whole budget for those two hours.
With prefetch a reservation is taken up to 150 s before its break, so a viewer who leaves before the break holds
budget for the same two hours. Measured:

- MediaTailor's `BEACON_FIRED` reached the siblings' log p50 8.0 s and p99 12.5 s after it fired (76 events, the
  persistent dev stage), and the delivery function wrote its record p50 8.4 s and p99 12.7 s after (52 records);
- over 14 days and every stage: 1,554 impressions, 775 line ads delivered, 0 late deliveries.

A reservation's impression therefore arrives within its retrieval lead (at most 150 s), the break (at most 120.12 s)
and the log's delay (13 s at p99): under 5 minutes. **Recommendation, for a later change:** release a reservation 10
minutes after it was made (twice that) with the sweeper every 5 minutes, so an unaired reservation returns its budget
within 15 minutes instead of 2 hours 15. The late path stays: MediaTailor retries a failed beacon for up to an hour,
and an impression after its release is still credited (`LATE`), so a retry costs at most one ad's price in
overspend. None has been seen.

## Consequences

- Per stage with the PDS, the internal load balancer (about $16 a month plus LCUs), three functions with their
  aliases, five IAM roles, and provisioned concurrency (about $1.45 an hour at 20/30/20 while channels run) are gone.
  Each Prebid Server task pays for 2 GiB more memory (about $0.009 an hour on Fargate). The stage gains an ECR
  repository and an image build when the sidecar's code or lock changes.
- The PDS has no cold start per request any more. Its capacity is the Prebid Server tasks', and a new task starts
  serving only once its sidecar is healthy.
- A sidecar that exits is restarted in place. Meanwhile Prebid Server's calls are refused: breaks are served SAFE with
  house ads, or the kit's house ads from the module's configuration.
- The PDS can no longer be called from outside its task. End-to-end tests reach it through the guidance's edge, as
  MediaTailor does, and read its decision records.
- The budgets: `/policy` returns to the design's 50 ms (group 75 ms) from 80/110; `/select` stays at 200 ms (group
  250 ms). The 400-session herds measured `/policy` at p99 12-18 ms (max 36 ms) at the module. `/select` is bound by
  its reservation's DynamoDB transaction tail (p99 to 125 ms): the module measured p99 65-134 ms, max 111-166 ms, and
  with a 150 ms budget one real-time 400-session break lost 1 pod in 400 to the auction's house ads. The design's
  150 ms waits for a cheaper reservation, not for the transport.
- Capacity: about 25 auctions/s per task at 0 fallbacks at the natural spread, the first fallbacks near 50/s
  ([demand.md](../demand.md#sizing-prebid-server-for-a-break) sizes tasks from that). A sub-second herd is served SAFE,
  never empty, and `<prefix>-pds-burst-real-time` says why.
- When AWS publishes the guidance as a construct library, the wrapper becomes ordinary composition.
