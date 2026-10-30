# 0035 — Prebid results for an ad server's VAST: one cached pod per break

**Status:** Proposed (accepted when M8-09's end-to-end leg passes)

## Context

With the demand module, MediaTailor sends each break's ad request to Prebid Server, which answers one VAST pod
([ADR 0020](0020-demand-through-the-prebid-server-guidance.md)). Many publishers run an ad server as well, such as
Google Ad Manager or FreeWheel. There the ad server decides the break: its direct campaigns compete with programmatic
demand, and Prebid's results reach it as key-values. Prebid's standard video flow for an ad server works like this:

1. the auction caches the winning VAST in Prebid Cache;
2. the ad server gets `hb_pb` (the price bucket), `hb_uuid` (the cache key) and `hb_bidder`;
3. a line item at that price bucket answers a VAST redirect creative, `https://<cache>/cache?uuid=%%PATTERN:hb_uuid%%`;
4. the player, here MediaTailor, follows it.

The owner asked for this path: Prebid results stored for later use in an ad server's VAST, through a cache that the
VAST can redirect to.

We needed to settle five things:

- **Where the auction runs.** MediaTailor can call one ADS per break, so the auction runs before the ad request: a
  PRE_ADS Function.
- **Which cache.** The Prebid Server guidance already deploys Prebid Cache: ElastiCache Serverless (Valkey) behind a
  cache function, with `/cache` rules on its public and internal load balancers. The guidance generates uuid4 keys,
  and a TTL bounds each entry.
- **How the pod survives.** The Pod Decision Service's `/policy`, `/select`, reservations and pod rules, and our
  trackers, must still decide what plays. Prebid's per-bid cache would store each bidder's `adm` alone, without the
  pod rules or our impression tracker.
- **What the public may reach.** The cache has to be readable from the internet, because MediaTailor follows a
  Wrapper with a plain GET. In the guidance, writes go through its CloudFront distribution as well.
- **Whether MediaTailor accepts a Wrapper that resolves to a pod.** On a throwaway stage it did. It parsed all 4 ads of
  a house pod behind a Wrapper and filled them exactly as it fills the pod without the Wrapper. It ignored the
  Wrapper's `allowMultipleAds="false"`.

## Decision

- **Configuration.** `modules.demand.ad_server` is `none` (the default: Prebid Server answers the pod, unchanged),
  `mock`, `gam` or `freewheel`. `ad_server_tag` holds the publisher's GAM or FreeWheel tag, with `{prebid}` where the
  key-values go. `pod_cache_seconds` defaults to 300. An ad server needs the release module, because the auction is
  a release Function.
- **Siblings.** The monetized pair siblings take the ad-server route. Each one:
  - GETs the ad server's tag, which carries `[player_params.hb_pb]`, `[player_params.hb_uuid]` and
    `[player_params.hb_bidder]`;
  - uses `FOLLOW_AD_SEQUENCE`, so the cached pod's standalones still back their slots.

  A third-party ad server gets no header of ours and never the origin secret. The probe sibling stays on Prebid
  Server, house only.
- **PRE_SESSION** blanks the three player parameters, so a value a player sends at session start never reaches the ad
  server.
- **The auction (PRE_ADS).** One `HTTP_REQUEST` per sibling POSTs to Prebid Server's auction, through its CloudFront
  and the origin rule. Its shape:

  | Part | Value |
  |---|---|
  | Query | the break's `evid`, `upid` and `dur`, as the direct route sends them |
  | Body | only the stored request `fast-adsrv-pod-<seconds>s` |
  | Headers | `X-Fast-Pc`, `X-Fast-Sid` and the viewer's `User-Agent` |
  | Added at apply | `X-Fast-Origin` and `X-Fast-Fv`, so the bundle never holds the secret |
  | Cache | `session_break`, keyed on the session and the event id, 30 s ([ADR 0034](0034-every-function-request-cached-by-scope.md)) |
  | Timeouts | 1,200 ms request, 1,300 ms tree budget; with the 2,000 ms ADS timeout the Live maximum needs 3,300 ms (the siblings have 6,000) |

  The root commits the answer's targeting to the three player parameters for this break, or empty values when there
  is no bid.
- **The pod.** The `fast-adsrv-pod-*` stored requests are the `fast-pod-*` pods plus two fields:
  `ext.fast.pod_cache.ttlseconds` and CTV price buckets in `ext.prebid.targeting`. The buckets are 0.10 up to $20,
  0.50 up to $50 and 1.00 up to $100. They set no `ext.prebid.cache`, so Prebid Server's core never caches single
  bids.
  - The fast-ctv module renders the pod that `/select` returns, exactly as on the direct route.
  - It stores that pod in Prebid Cache once, through Prebid Server's own cache client.
  - It answers JSON: one bid whose `ext.prebid.targeting` holds `hb_pb` (the bucket of the sum of the paid winners'
    prices), `hb_uuid`, `hb_cache_id`, `hb_bidder` (`fast-ctv`), `hb_cache_host` and `hb_cache_path`.
  - A pod of house ads only, an empty pod or a failed write answers no bid, and the ad server serves its own house
    ad.
- **The cache.** Writes go to `cache.internal`, the private record `cache.<prefix>.fast.internal`. It aliases the
  guidance's internal load balancer, which admits only the Prebid Server tasks. The targeting names the public host.
  The origin rule group opens `/cache` only for `GET` or `HEAD` with a uuid4 `uuid`, and blocks every other form,
  public writes included.
- **The ad server.**
  - `mock` is a small GAM-like ad server for the demo. It sits behind an HTTP API, and its handler requires the
    origin secret. Its Prebid line items, one per bucket, answer a Wrapper (`allowMultipleAds="true"`) to the cached
    pod, and its house line answers one to the stage's house pod.
  - `gam` and `freewheel` are configuration plus a documented line-item setup. They cannot be tested without
    accounts.

## Consequences

- With `ad_server: none` every rendered file and every template is unchanged.
- The ad server decides each break as one unit. Its line item sees one price for the whole pod, so it cannot pick ads
  inside the pod. The pod's composition, its separation rules and its reservations stay the Pod Decision Service's.
  Prebid's long-form route would let the ad server assemble the pod per ad (`hb_pb_cat_dur`), but it would bypass
  those rules.
- **Latency.** A break waits for the auction before its ad request (up to 1,300 ms of PRE_ADS), then the ad server and
  the cache read. A write that misses the exitpoint's extra 150 ms leaves the break without a cached pod. That
  break answers no bid, and the ad server serves its house ad.
- **The cache function needs ready instances.** The guidance's function, at its 256 MB and without provisioned
  concurrency, lost 17 of 100 pods in a 100-session break. It scaled to 31 instances, each about 1.4 s to start.
  - Provisioned concurrency alone did not help at 256 MB. The guidance's handler opens its cache connection on an
    instance's first request, and that took about 1 s with the CPU of 256 MB.
  - With 1,769 MB (one vCPU) and 8 provisioned instances, the same break cached 100 of 100 pods (write p99 104 ms),
    and five prefetched breaks cached every pod except in a break asked for twice.
  - The wrapper sets both, in the ad-server mode only. The cost is about $0.17 an hour while the stage exists.
  - A Lambda function stays the cache server: it meets the deadline with ready instances.
- **No viewer IP.** The auction is MediaTailor's Function request, which carries no viewer IP, so `/policy` sets no
  `device.ip` on this route. The direct route keeps it.
- **The origin secret sits in the auction Function.** A rotation must therefore re-create the trees that carry the old
  value under a new release before that value is retired. The release module's rotation does this:
  - its switch creates each kept tree that carries the secret again under a new release, with the new value;
  - each such Function is tagged with its slot (`fast:origin-slot`);
  - its retire refuses while a kept Function still carries the old value.
- **What the ad server sees.** A third-party ad server gets the targeting and its own tag's parameters, nothing of
  ours.
- **The Prebid Cache TTL** (300 s) outlives a prefetch's retrieval-to-break gap (about 140 s) plus the break, so
  MediaTailor can follow the Wrapper whenever it retrieves.
