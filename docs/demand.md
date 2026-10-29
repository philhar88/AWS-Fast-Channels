# Demand: Prebid Server through the AWS guidance

The optional demand module (`modules.demand`, off by default) puts an OpenRTB auction behind the channel's ad
breaks. MediaTailor POSTs a templated OpenRTB request to Prebid Server, deployed from AWS's [Guidance for Deploying a
Prebid Server on AWS](https://github.com/aws-solutions-library-samples/prebid-server-deployment-on-aws), and gets back
one VAST 4.2 pod. The decision is recorded in [ADR-0020](adr/0020-demand-through-the-prebid-server-guidance.md).

**Status (Phase 1, in progress):** the pinned guidance with its overlays, the Prebid Server configuration renderer
and the fast-ctv module are in place. The stacks and the deploy and destroy scripts follow; until they land,
enabling `modules.demand` deploys nothing.

- [How a break is sold](#how-a-break-is-sold)
- [The guidance and its overlays](#the-guidance-and-its-overlays)
- [Prebid Server configuration](#prebid-server-configuration)
- [The fast-ctv module](#the-fast-ctv-module)
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
| O1 | `source/infrastructure/app.py` | CDK context `stackPrefix` (stacks `<prefix>-pbs`, `<prefix>-pbs-sim`) and a dedicated bootstrap (`bootstrapQualifier`, `fileAssetsBucketName`, `imageAssetsRepositoryName`) | The shipped app hard-codes its stack names and the default bootstrap qualifier, whose execution role is often AdministratorAccess |
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
| Impressions | Every rendered ad has an impression, and ours, even a third-party ad with none of its own |
| Durations | Every break length on the grid has a pod; 31 s and 300 s are refused with 400 |

## Rules the configuration relies on

- **The body-template rule.** MediaTailor inserts template values raw, without JSON escaping. A `"` or `\` in a
  value, or an unresolved unquoted number, makes the body invalid, and MediaTailor then sends no request at all. The
  body therefore carries only constants and MediaTailor's own variables, each inside a JSON string. The playback
  ticket and every player parameter travel in header templates, which Prebid Server never copies to bidders.
- **The no-hook contract.** A stored request names only the own book. If the module is disabled or absent, or
  `/policy` is unreachable, slow or failing, the auction calls no third-party bidder.
- **The 30.03 s grid.** MediaTailor rounds `session.avail_duration_secs` half up and sends 300 for a missing duration.
  A break off the grid names a stored request that does not exist: Prebid Server answers 400 and the slate plays.
