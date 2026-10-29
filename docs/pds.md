# Pod Decision Service

The Pod Decision Service (PDS) decides, for every ad break of every monetized session, what Prebid Server may ask
third parties and which ads make up the pod. It is part of the optional ad-monetization modules (`modules.pds`, off by
default; see [operations.md](operations.md#configuration-reference)), which are being built on the
`feature/agentic-monetization` branch. The rules and the contract live in `fast_channels.core.pds`; the service
that runs them beside Prebid Server is [described below](#the-service). `/policy` is served; `/select` and the own
book follow.

- [The two calls](#the-two-calls)
- [Contract](#contract)
- [Privacy modes](#privacy-modes)
- [The viewer-IP rule](#the-viewer-ip-rule)
- [Device type](#device-type)
- [Pod rules](#pod-rules)
- [Trackers](#trackers)
- [Decision records](#decision-records)
- [Buckets](#buckets)
- [Patch contributors](#patch-contributors)
- [The service](#the-service)

## The two calls

MediaTailor sends each ad request as a templated OpenRTB POST to Prebid Server. The stored request it names lists one
pod of 30 s slots and only the own book as a bidder. The fast-ctv Prebid Server module then calls the PDS twice:

1. **`POST /policy`**, before the auction, with the session id, the playback configuration, the break's SCTE-35 event
   id, UPID and duration, the viewer's User-Agent and the X-Forwarded-For chain. The answer is a patch the module applies
   verbatim: bidders to add, `regs`, `device.ip`/`devicetype`, `app.content` fields and, in FULL mode only, user data
   and deals. It never removes the own book.
2. **`POST /select`**, after the auction, with every bid and what the module read from its `adm`. The answer is the
   pod: the sequenced ads, a standalone per slot for MediaTailor's ad buffet, and the PDS's impression tracker for each
   ad. The module renders exactly that pod as one VAST 4.2 document.

Both calls fail safe. If `/policy` is slow, failing or unreachable, the stored request stays as it is: own book and
house ads, no third party, no viewer data. If `/select` fails, the module renders a pod of the auction's house bids.

## Contract

| Call | Schema | Examples |
|---|---|---|
| `/policy` | [`contracts/pds-policy.schema.json`](../contracts/pds-policy.schema.json) | [`contracts/fixtures/pds/policy/`](../contracts/fixtures/pds/policy) |
| `/select` | [`contracts/pds-select.schema.json`](../contracts/pds-select.schema.json) | [`contracts/fixtures/pds/select/`](../contracts/fixtures/pds/select) |

- Both are generated from the pydantic models in `fast_channels.core.pds.models` by
  `scripts/generate_contracts.py`; CI fails when they are out of date.
- Every request and response carries `v: 1`, and both sides refuse unknown fields. A change to the contract is a new
  version: a module and a PDS that disagree fail visibly, and safely.
- `decision_id` is `<sid>.<evid>`, the MediaTailor session id and the break's event id. It joins decision records,
  trackers, delivery and the ledger, so both are required: a request without them is refused, and the break is served
  by the fail-safe path.
- Each example holds a `description`, the PDS-side `context` (snapshot values the request does not carry), the
  `request` and the `response`. The `response` is regenerated from the rules, so the examples double as golden tests on
  both sides of the contract.
- A null `device.ip` or `device.ipv6` in the patch means *remove it*: Prebid Server would otherwise take the leftmost
  X-Forwarded-For entry, which the client controls.

## Privacy modes

| Mode | Trigger | What third parties receive |
|---|---|---|
| FULL | A consented adult with separate video-privacy consent | Per-partner pseudonymous ids, user data scoped per bidder |
| PSEUDONYMOUS | Authenticated, no sale/share opt-out | No ids; genre and category, never the title |
| CONTEXTUAL | Guests, ticketless sessions, invalid or replayed tickets, opted-out viewers | No ids, no user data, coarse geo; show-level content metadata; GPP where known |
| SAFE | The consent store failed or is older than 60 s; probe sessions | Nothing: own book and house ads only (house ads only for probes) |
| KIDS | A kids playback configuration (`…-p-k`, `…-p-l`) or a child-directed channel | Nothing: COPPA set, house ads and creatives approved for children only |

The PDS resolves a break's mode in this order (`fast_channels.core.pds.policy.resolve`):

1. a kids configuration or child-directed channel is KIDS, even for a probe, so COPPA is always set there; any other
   probe session is SAFE (house ads only);
2. a ticket that is missing, malformed, unknown, bound to another session, minted for another configuration, or past
   its lease gives CONTEXTUAL, with a reason code (`missing`, `invalid`, `unknown`, `replay`, `pc_mismatch`,
   `expired_or_raced`) and **every viewer parameter zeroed**;
3. a bound viewer whose consent cannot be read gives SAFE (`consent_unavailable`);
4. otherwise the consent decides: FULL, PSEUDONYMOUS, or CONTEXTUAL (`opted_out`, keeping only the GPP string).

Until viewer identity exists, no session carries a ticket, so every session is CONTEXTUAL with reason `missing`.
Prebid Server's account Activity Controls do the per-field disclosure for each mode.

## The viewer-IP rule

MediaTailor's `{{session.client_ip}}` is the leftmost X-Forwarded-For entry, and a client sets it by sending its own
header: a client-sent `X-Forwarded-For: 203.0.113.7` was reported as the client IP. It is never used. Each hop instead
appends its TCP peer, so by the time an ad request reaches Prebid Server the chain reads, left to right:

```
[client-supplied, …] viewer [, viewer-side CloudFront], MediaTailor egress, PBS-side CloudFront
```

`fast_channels.core.pds.xff.viewer_ip` walks it from the right:

1. drop the PBS-side CloudFront hop that the load balancer appended. The guidance's load balancer admits only
   CloudFront's origin-facing addresses carrying its CloudFront's secret header, so this hop must be one of those
   addresses;
2. drop MediaTailor's egress. It cannot be recognised by address range (MediaTailor used 18 different EC2 addresses
   with no service tag in one test), so it is found by position, or, when the PBS CloudFront forwards
   `CloudFront-Viewer-Address`, as that address's rightmost occurrence;
3. drop the viewer-side CloudFront hop if the next entry is one of CloudFront's published origin-facing addresses
   (`CLOUDFRONT_ORIGIN_FACING` in `ip-ranges.json`); a session opened directly on MediaTailor has none;
4. the next entry is the viewer.

A chain that does not fit gives no address (`no_chain`, `short_chain`, `egress_not_found`, `unexpected_hop`,
`invalid_entry`, `not_global`), never a guess. The address is not authenticated: it is what the chain says the viewer's connection came
from.

## Device type

`device.devicetype` (AdCOM 1.0) comes from the viewer's User-Agent (`fast_channels.core.pds.devicetype`): Connected TV
only on an explicit TV token (Samsung, LG, Sony, Hisense and Vizio sets, HbbTV, Roku, Fire TV, Apple TV, Chromecast,
Android TV), then set-top boxes, game consoles (Connected Device), tablets, phones and computers. A browser, the
reference player included, is never declared CTV. A User-Agent no rule recognises gives none, and so does an Android
one with neither "Mobile" nor a tablet token, which many TV boxes send.

## Pod rules

`fast_channels.core.pds.pod.select_pod` is deterministic and exact for the stored requests' equal 30 s slots.

| Step | Rule |
|---|---|
| Eligibility | Own book only in SAFE, KIDS, or when the module applied no policy; in KIDS only house ads and creatives approved for children; house ads only on probe sessions. A house ad is an own-book bid marked house: the mark from any other bidder is ignored. Usable VAST (InLine or Wrapper) with a known duration; a declared `dur` must agree with it (rounded half up) and a missing one is taken from it; the slot must accept it (`rqddurs`). A `crid`, and an `adomain` unless it is a house ad. No creative the session saw in its recent breaks. At or above the floor, except house ads (fillers) and tier-0 lines (contracted) |
| Value | Tier-0 lines first (sponsorships, guaranteed lines, PG deals). Then paid bids by accepted net value, `price / 1000 × accept_ratio(bidder) − c_ins` with `c_ins` = $0.0005 per insertion, which must beat the slot's ad-load price λ (0 until the ad-load model exists, `fast_channels.core.adload`). House ads fill the rest; they are never sold |
| Placement | Each bid stays in its own imp's slot. Best candidates first; one is placed if its slot is free, it repeats no creative, advertiser domain or IAB category already in the pod (across bidders; house ads only never repeat a creative), and Σ `dur` ≤ `poddur` and Σ VAST durations ≤ the break still hold |
| Standalones | Per winner, the best remaining candidate of the same slot and duration. MediaTailor's ad buffet plays it in that slot if the winner fails, for example a creative it has not transcoded yet. The pod stays within the break even if every winner that has a standalone fails |
| Leftover | Slots nothing fills are not returned. Channel Assembly's break slate plays them; at a personalization threshold of 121 s MediaTailor keeps the partial pod |
| Short avails | An avail under 30 s (the tail of a break a session joined late) gets an empty pod |

The break is the duration MediaTailor requested, with one exception: a prefetch retrieval carries the duration rounded
to whole seconds (60,000 ms for a 60,060 ms break). When the requested duration is exactly the schedule of record's
duration rounded, the schedule's duration is the limit, so two 30.03 s ads fit a prefetched 60 s break.

Worked example (the demo demand, one FULL session, a 120.12 s break; `contracts/fixtures/pds/select/demo-full-120s.json`):

| Slot | Bids | Winner | Standalone |
|---|---|---|---|
| 1 | `direct1` tier-0 line $8, `sim1` $12.50, `house1` | `direct1` | `sim1` |
| 2 | `sim2` $22.50, `house2` | `sim2` | — (`house2` won slot 4) |
| 3 | `sim3` $35 (brand-c), `house1` | `sim3` | `house1` |
| 4 | `sim4` $12.50 (brand-c), `house2` | `house2` (`sim4` repeats brand-c) | — |

The same demand without the simulator (own book only) gives `direct1, house2, house1` and the slate for the last
30.03 s. Every bid's outcome (`won`, `standalone`, or why not) goes into the decision record.

## Trackers

Every ad in the answer gets the PDS's own impression tracker, the CDN's no-store beacon:

```
https://<cdn>/ads/b.gif?d=<sid>.<evid>&c=<crid>&e=impression&enr=<0|1>
```

MediaTailor fires it server-side and logs each firing as a `BEACON_FIRED` event with these parameters, which is how
delivery is counted: fills deduplicated by `(sid, evid)`, ad impressions by `(sid, evid, crid)`, because a pod holds
several ads. The counterparty's count is what bills; ours is a seller-side server count. `enr` is the content-enrichment
arm of the break.

## Decision records

`fast_channels.core.pds.records` defines one JSON record per call, joined on `decision_id`:

- **`pds.policy`**: mode and reason, the bidders added, the device type, the viewer-IP rule's outcome, the catalog
  context's length, the patch contributors applied or skipped and their record fields, latency;
- **`pds.select`**: the break and its budget, every bid at or above the floor with its outcome (bids below it are
  counted), the pod, the standalones, the filled duration, latency.

Records never hold the ticket, the viewer's address, User-Agent or headers, or the catalog context itself.

## Buckets

Arms and holdouts use `bucket(seed, key) = SHA-256(seed ‖ key) mod 100` (`fast_channels.core.pds.buckets`), with
versioned, non-secret seeds, so the offline simulator and holdout audits recompute every assignment from the records:
`arm-v1` per viewer, and `crawl-v1` per session-break (`<sid>.<evid>`) for the content-enrichment holdout.

## Patch contributors

Other modules add `app.content` fields and decision-record fields through the `PatchContributor` interface
(`fast_channels.core.pds.contributors`) without the PDS knowing them; the content-enrichment module contributes the
show's metadata this way. Contributions fail open: a contributor that raises, or that sets a field an earlier one set,
is skipped and named in the record.

## The service

`FastChannels-<prefix>-Pds` runs in the Prebid Server guidance's VPC. Like the Demand stack it needs the guidance's
network, so `scripts/prebid_guidance.py deploy` deploys it right after the Demand stack, whose private zone it adds a
record to, and `destroy` deletes it first ([docs/demand.md](demand.md#deploy-and-destroy)). Its state is the Data
stack's: the table `<prefix>-pds` and the private config bucket exist with `modules.pds.enabled` and stay until
`make destroy`. The Data stack writes the config and lake bucket names to `/<prefix>/pds/config-bucket` and
`/<prefix>/pds/lake-bucket`, which the Pds stack resolves at deploy: it takes no cross-stack reference to the Data
stack, whose own deploys (`make deploy`) never see the guidance's network.

| Part | What it is |
|---|---|
| Load balancer | An internal Application Load Balancer in the guidance's private subnets at `pds.<prefix>.fast.internal`, HTTP on port 80. Its security group admits only the Prebid Server tasks' security group. `POST /policy` goes to its function; anything else gets a 404 |
| `/policy` function | Python 3.14 on arm64, 1,024 MB, outside the VPC: the load balancer invokes it, so it has no network-interface cold start. The load balancer calls its `live` alias. It answers with `decide()` and the current snapshot, logs one `pds.policy` decision record and the metrics `PolicyDecisions` (by `mode`), `PolicyNoViewerIp`, `PolicyRefused` (a 400) and `PolicyErrors` (a 500) in the `FastChannels` namespace, each by `service` |
| Census gate | `<prefix>-pds-census-gate`, Step Functions with JSONata and SDK calls only, on `Census Changed`. When the first channel starts it sets `modules.pds.provisioned_concurrency` on every function's `live` alias; 15 minutes after the last one stops, if none runs then, it removes it. `deploy-demand` runs it once with the census's current count. With `provisioned_concurrency: 0` every run removes it, so lowering the setting also removes what was set |
| Snapshot | `snapshot/current.json` in the private config bucket (never the CDN, never the ingest bucket), with each version kept 30 days under `snapshot/history/`. It holds the demand allow-list (in demo mode the guidance's bidder simulator, `amt`), shed bidders, the floor and CloudFront's origin-facing networks from `ip-ranges.json`, which the viewer-IP rule needs. The publisher writes it when the stack deploys and every hour, and only when its content changed. Each function reads it during its init and then every 30 seconds in the background with a conditional GET, so no request waits on S3; one that has never read it serves no third-party bidder and no viewer address |
| Decision records | A subscription on the function's log group sends the `pds.policy` lines to the Firehose stream `<prefix>-pds-policy`, which writes them to the lake bucket under `pds/policy/<yyyy>/<MM>/<dd>/<HH>/`, within a minute. The lake table `pds_policy` reads them |

Why in the VPC and warm: `/policy` gets 50 ms. Reached over API Gateway and a NAT gateway, a third of MediaTailor's
auctions fell back to the own book in a live test, every first break among them, because the function and its
connections were cold.

```sql
SELECT mode, reason, ip_rule, count(*) AS breaks, approx_percentile(latency_ms, 0.99) AS p99_ms
FROM "<prefix>_lake".pds_policy
WHERE datehour >= date_format(current_timestamp - interval '1' hour, '%Y/%m/%d/%H')
GROUP BY 1, 2, 3
```
