# Delivery and multi-CDN

One Amazon CloudFront distribution delivers everything viewers fetch: MPEG-TS segments and ad media from the private
media bucket through origin access control (OAC), and MediaTailor's SSAI manifests and ad redirects. One routing
table in the code drives both the CloudFront behaviours and a CDN-neutral *path contract* that other CDNs can copy.

- [Routing table](#routing-table)
- [Origins and access control](#origins-and-access-control)
- [Headers, CORS and timing](#headers-cors-and-timing)
- [Access logs](#access-logs)
- [The path contract](#the-path-contract)
- [A CDN-neutral content domain](#a-cdn-neutral-content-domain)
- [Multi-CDN in v3.0](#multi-cdn-in-v30)
- [Capacity](#capacity)

## Routing table

`fast_channels.infra.constructs.delivery.ROUTES`, in precedence order (first match wins). MediaTailor paths come
before any S3 glob.

| # | Path | Origin | Cache policy | Origin request policy | Response headers | Compress | Methods | Purpose |
|---|---|---|---|---|---|---|---|---|
| 1 | `/tm/*` | mediatailor-segments | Managed-CachingOptimizedForUncompressedObjects | none (cache key only) | media | no | read | Ad segments MediaTailor transcoded (external VAST creatives) |
| 2 | `/v1/*` | mediatailor-manifests | Managed-CachingDisabled | Managed-AllViewerAndCloudFrontHeaders-2022-06 | preflight | yes | all | SSAI sessions: multivariant (/v1/master), media playlists (/v1/manifest, gzip), POST /v1/session, tracking, ad segment redirects (/v1/segment) |
| 3 | `*` (default) | media | Managed-CachingOptimizedForUncompressedObjects | Managed-CORS-S3Origin | media | no | read | MPEG-TS content and preconditioned ad segments; ad manifest copies, VAST pods and beacons (/ads); EPG feeds (/epg); content manifests under /assets are denied by the bucket policy |

"read" is `GET`, `HEAD`, `OPTIONS`; "all" adds the methods MediaTailor's explicit session start (`POST`) needs. The
table is MediaTailor's
[production CloudFront configuration](https://docs.aws.amazon.com/mediatailor/latest/ug/cf-comprehensive-configuration.html)
without its SGAI route (`/v1/i-media/*`); MediaTailor's server-side beacon redirects are `/v1/segment/*` requests,
which route 2 covers. Every route was confirmed live through the `cache-behavior-path-pattern` field of the access
logs, including gzip on the media playlists.

Rules behind the table:

- **Forward every viewer header to MediaTailor, and only the CORS headers to S3.** Route 2 forwards all viewer
  headers, cookies and query strings, including the four that MediaTailor
  [requires from a CDN](https://docs.aws.amazon.com/mediatailor/latest/ug/cdn-configuration.html#cdn-required-headers):
  `Accept-Encoding` (gzip), `User-Agent` and `X-Forwarded-For` (ad targeting) and `Host` (request routing). The
  query strings carry the ADS variables and `aws.*` session options. Nothing on route 2 is cached, so this cannot
  fragment a cache. The default route never forwards the viewer `Host` to S3.
- **Never cache** `/v1/master`, `/v1/session` or `/v1/segment` responses: they create sessions or expire.
- **Content segments are immutable**: a new encode is a new key under a new ladder version (the pipeline refuses to
  encode a `READY` asset again in place), so long TTLs are safe.
  Mutable objects on the default route (EPG feeds, VAST pods) are written with a short `Cache-Control`, which the
  cache policy honours.
- The distribution uses managed policies plus exactly one custom response headers policy and no custom cache policy;
  accounts have only 20 of each.

## Origins and access control

| Origin id | Host | Access |
|---|---|---|
| `media` | the media bucket | `S3BucketOrigin.with_origin_access_control` (read). The bucket is private |
| `mediatailor-manifests` | `manifests.mediatailor.<region>.amazonaws.com` | Public MediaTailor endpoint; HTTPS only, TLS 1.2, 10 s read timeout, 3 connection attempts |
| `mediatailor-segments` | `segments.mediatailor.<region>.amazonaws.com` | Same |
| `catalog` (only with `modules.crawl`) | the catalog-context bucket | OAC read of `catalog/ctx/*` and list, for this distribution only; route `/catalog/ctx/*`, GET and HEAD ([content-enrichment.md](content-enrichment.md#serving-and-misses)) |

The Data stack's bucket policy admits CloudFront distributions of this account and **denies them `assets/*.m3u8`**:
content manifests are for Channel Assembly only, so an asset cannot simply be played from the CDN by
its VOD manifest (verified: 403). Segments, ad manifests under `ads/`, VAST pods and EPG feeds stay readable. That is
why Finalize copies each preconditioned ad's manifests under `ads/<ladder>/<creative_id>/`, with absolute segment
URIs into `assets/`: MediaTailor reads the creative named in the VAST pod through the CDN, then stitches its segments
into SSAI sessions through `/v1/segment` redirects ([ad-insertion.md](ad-insertion.md#preconditioned-ads)). The deny
stops casual direct playback, not a determined viewer: content segments have predictable names
(`assets/<asset>/<ladder>/main_<rendition>_<nnnnn>.ts`, and every SSAI playlist shows the prefix), so an ad-free
playlist can be rebuilt. If that matters, protect `/assets/*` with CloudFront signed cookies or URLs, or a token check
at the edge.

MediaTailor has no CloudFront origin access control type, so the two MediaTailor origins are its public endpoints.

The distribution serves HTTP/2 and HTTP/3 over IPv4 and IPv6, HTTPS only, with `delivery.price_class`. With
`delivery.content_domain` and `delivery.certificate_arn` (an ACM certificate in us-east-1) it also answers on your
domain with the `TLSv1.2_2025` security policy. There is no AWS WAF web ACL in v3.0 (it bills per request, and a FAST
audience fetches a segment every few seconds) and no geo restriction; add either where your rights or threat model
require it.

## Headers, CORS and timing

Two response headers policies:

- **`media`** (custom, routes 1 and 3): `Access-Control-Allow-Origin: *` *overriding* the origin's value,
  `Access-Control-Allow-Headers: *` (CMCD header mode preflights), `Timing-Allow-Origin: *` and `Server-Timing` on
  every response. MediaTailor echoes the request's `Origin` with credentials, which a shared cache must never replay
  to other sites, hence the override on the cached MediaTailor route. `Server-Timing` carries `cdn-rid`, the same
  value as the `x-edge-request-id` log field, so a player can quote the request id of a failed segment.
- **`preflight`** (managed CORS-with-preflight-and-security-headers, route 2): uncached MediaTailor responses keep
  their own CORS headers.

## Access logs

`CdnAccessLogs` delivers CloudFront standard logs v2 (CloudWatch Logs vended delivery) as Parquet to the logs bucket,
with Hive-style partitions:

```
s3://<LogsBucketName>/AWSLogs/aws-account-id=<acct>/CloudFront/DistributionId=<id>/year=YYYY/month=MM/day=DD/hour=HH/
```

Records include the request id, cache result, timing (`time-to-first-byte`, `origin-fbl`), the matched
`cache-behavior-path-pattern`, the full query string (ADS requests carry `ch`, `sid`, `evid`, `upid`) and every CMCD
field (`cmcd-session-id` joins MediaTailor's session id when players send CMCD). Parquet column names use `_` for `-`,
`(` and `)` (`cache_behavior_path_pattern`, `cs_User_Agent`); query them with Amazon Athena. Because CloudFront's
delivery source exists only in us-east-1, deployments in other regions get these resources in a `…-Edge-<region>`
stack there.

## The path contract

The same `ROUTES` table is published as JSON for configuring another CDN in front of the same origins:

- [`docs/path-contract.json`](path-contract.json) in the repository (placeholders for the distribution and region);
- the `PathContract` output of the Media stack for a deployed stage (real hosts).

It lists the three origins (other CDNs chain the `media` origin to the CloudFront domain, because the bucket admits
only CloudFront), every rule with its methods, cache key, TTL bounds, forwarded headers and compression, and the two
CORS header sets. The file is generated from the code; regenerate it with
`uv run pytest infra/tests/test_delivery.py --snapshot-update` after changing `ROUTES`.

## A CDN-neutral content domain

`delivery.content_domain` (with `delivery.certificate_arn`) gives segments a host name that is yours rather than
CloudFront's. It becomes:

- an alternate domain name of the distribution;
- Channel Assembly's `DefaultSegmentDeliveryConfiguration.BaseUrl` (every content segment URL in every channel
  manifest);
- the playback configurations' `AdSegmentUrlPrefix` and `ContentSegmentUrlPrefix`;
- the host of the VAST pods, ad beacons and ad manifest copies.

Players therefore only ever receive URLs that are relative or on the content domain, and moving that domain's DNS to
another CDN moves all segment traffic without touching MediaTailor. The ladder kits' fingerprint includes the content
host, so changing `content_domain` re-encodes every kit on the next deploy (minutes, about $0.25 per ladder) and gives
the house ads new creative ids.

## Multi-CDN in v3.0

What works:

- **Segments on several CDNs** by DNS or traffic steering of `delivery.content_domain`, with each CDN configured from
  the path contract and chaining to CloudFront for the `media` origin.
- **Manifests through another CDN**: a CDN that proxies the MediaTailor routes (rules 2–3) to the MediaTailor origins
  or to CloudFront serves the same sessions. List such CDNs in `delivery.cdns` (`name`, `manifest_base`,
  `content_base`): `channels___get_channel` then returns the playback URLs of every output through each of them
  (`cdn_playback`, the deployment's own distribution first as `cloudfront`), and the console's Channels view lists
  them for copying.

```json
"delivery": {
  "content_domain": "media.example.com",
  "certificate_arn": "arn:aws:acm:us-east-1:111122223333:certificate/…",
  "cdns": [{"name": "cdn-b", "manifest_base": "https://cdn-b.example.net", "content_base": "https://media.example.com"}]
}
```

Limits:

- Channel Assembly writes absolute segment URLs from its default segment delivery configuration, and ad insertion does
  not rewrite absolute origin URLs (`ContentSegmentUrlPrefix` only rewrites relative ones). Per-session CDN choice
  through `ConfigurationAliases` and `[player_params.cdn]` would therefore only move *ad* segments.
- Channel Assembly generates the multivariant playlist, so HLS Content Steering tags cannot be injected without a
  manifest manipulator.
- The Media stack's `Channels` output and the console's player use the primary CDN; the alternatives are listed,
  not steered.
- A CDN-shielded mode (CloudFront between MediaTailor and Channel Assembly) needs a public channel policy, so it is not
  offered ([ADR-0007](adr/0007-sigv4-channel-origin-no-cdn-shield.md)).

## Capacity

Segment delivery scales with CloudFront. The limit to plan for is MediaTailor's **Ad Insertion requests** quota, not
Channel Assembly's manifest quota.

**Channel Assembly load levels off.** MediaTailor SSAI reuses Channel Assembly responses across sessions for up to the
origin's `Cache-Control` `max-age`, which is about 2 s for media playlists and 60 s for the multivariant playlist.

The measured load:

| Concurrent SSAI sessions | 5 | 25 | 50 | 100 | 200 | 400 | 800 | 1,600 |
|---|---|---|---|---|---|---|---|---|
| Channel Assembly requests per second | 0.8 | 2.6 | 3.9 | 5.0 | 5.9 | 6.9 | 7.9 | 8.0 |
| Share of player manifest requests | 95 % | 63 % | 47 % | 30 % | 18 % | 11 % | 6 % | 3 % |

How it was measured:
- One BASIC channel with the `standard` ladder (3 variants), through CloudFront, on 2026-10-28.
- Load levels off near 8.6 requests per second, about 2.9 per variant playlist. That is far below the 50 per channel.
- Reuse was the same with one User-Agent for every session, with a unique User-Agent per session, and with players that
  call MediaTailor directly.
- The load came from one Region through one CloudFront edge location. A geographically spread audience has not been load
  tested.

**What binds.** Each SSAI viewer makes about 0.19 MediaTailor requests per second:
- the media playlist every 6 s target duration;
- one `/v1/segment` redirect per ad segment. At 8 ad minutes an hour, these are about 12 % of the total.

MediaTailor's `Requests` metric counts both.

| Quota (default) | Concurrent SSAI viewers at 100 % | At 70 % |
|---|---|---|
| MediaTailor Ad Insertion requests (10,000 per second per account) | about 53,000 | about **37,000** |
| Channel Assembly manifest requests per channel (50 per second) | not reached in the test; at least 9,970 (conservative bound) | at least 6,980 |
| Channel Assembly manifest requests per account (400 per second) | binds only with many busy channels: about 6,200 across 100 lightly watched channels | about 3,100 |

Above 1,600 sessions per channel, only the conservative bound has been measured. To serve more:

1. **Raise the Ad Insertion requests quota before launch.** For audiences above about 37,000, request an increase in
   Service Quotas. The quota has no usage metric, so watch MediaTailor's `Requests` metric.
2. **Keep the channel alarm.** The `<channel>-manifest-requests` alarm fires when a channel stays at 80 % of 50 TPS
   (2,400 requests per minute) for three minutes. If you raise that quota, set `channel_manifest_tps_quota` on
   `MediaObservability` to the new value so the alarm follows it.
3. **Make fewer playlist reloads per viewer** by using longer segments in a custom ladder.
