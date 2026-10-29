# Pod Decision Service

The Pod Decision Service (PDS) decides, for every ad break of every monetized session, what Prebid Server may ask
third parties and which ads make up the pod. It is part of the optional ad-monetization modules (`modules.pds`, off by
default; see [operations.md](operations.md#configuration-reference)), which are being built on the
`feature/agentic-monetization` branch. This page describes the rules and the contract, which live in
`fast_channels.core.pds` and are complete; the service that runs them behind Prebid Server is added by the module's
next changes.

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
