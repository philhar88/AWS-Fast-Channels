# Delivery and multi-CDN

One Amazon CloudFront distribution delivers everything viewers fetch: CMAF segments and ad media from the private
media bucket through origin access control (OAC), and MediaTailor's SSAI and SGAI manifests. One routing table in the
code drives both the CloudFront behaviours and a CDN-neutral *path contract* that other CDNs can copy.

- [Routing table](#routing-table)
- [Origins and access control](#origins-and-access-control)
- [Headers, CORS and timing](#headers-cors-and-timing)
- [Access logs](#access-logs)
- [The path contract](#the-path-contract)
- [A CDN-neutral content domain](#a-cdn-neutral-content-domain)
- [Multi-CDN in v3.0](#multi-cdn-in-v30)
- [Requests that bypass the CDN](#requests-that-bypass-the-cdn)
- [Capacity](#capacity)

## Routing table

`fast_channels.infra.constructs.delivery.ROUTES`, in precedence order (first match wins). MediaTailor paths come
before any S3 glob.

| # | Path | Origin | Cache policy | Origin request policy | Response headers | Compress | Methods | Purpose |
|---|---|---|---|---|---|---|---|---|
| 1 | `/tm/*` | mediatailor-segments | Managed-CachingOptimizedForUncompressedObjects | none (cache key only) | media | no | read | Ad segments MediaTailor transcoded (external VAST creatives) |
| 2 | `/preconditioned-ads/*` | mediatailor-segments | Managed-CachingOptimizedForUncompressedObjects | none (cache key only) | media | no | read | Preconditioned ad playlists MediaTailor re-hosts for SGAI asset lists |
| 3 | `/v1/i-media/*` | mediatailor-manifests | UseOriginCacheControlHeaders-QueryStrings | none (cache key only) | media | yes | read | SGAI media playlists: identical across sessions, cached for the origin's max-age (1 s) |
| 4 | `/v1/dash/*` | mediatailor-manifests | Managed-CachingDisabled | Managed-AllViewerAndCloudFrontHeaders-2022-06 | preflight | yes | read | SSAI DASH manifests (per session, gzip) |
| 5 | `/v1/manifest/*` | mediatailor-manifests | Managed-CachingDisabled | Managed-AllViewerAndCloudFrontHeaders-2022-06 | preflight | yes | read | SSAI HLS media playlists (per session, gzip) |
| 6 | `/v1/*` | mediatailor-manifests | Managed-CachingDisabled | Managed-Elemental-MediaTailor-PersonalizedManifests | preflight | yes | all | Session start (/v1/master, POST /v1/session), tracking, beacon redirects, interstitial asset lists |
| 7 | `*` (default) | media | Managed-CachingOptimizedForUncompressedObjects | Managed-CORS-S3Origin | media | no | read | CMAF content and preconditioned ad segments, VAST pods and beacons (/ads), EPG feeds (/epg); content manifests under /assets are denied by the bucket policy |

"read" is `GET`, `HEAD`, `OPTIONS`; "all" adds the methods MediaTailor's explicit session start (`POST`) needs. Every
route was confirmed live through the `cache-behavior-path-pattern` field of the access logs, including gzip on
`/v1/dash/*` and `/v1/manifest/*` and cache hits on `/v1/i-media/*`.

Rules behind the table:

- **Never forward the viewer `Host` to S3 or Channel Assembly.** The default route forwards only the CORS headers;
  route 6 uses MediaTailor's managed policy, which forwards `User-Agent` and `X-Forwarded-For` (ad targeting) and all
  query strings (ADS variables, `aws.*` session options), but not `Host`.
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

The Data stack's bucket policy admits CloudFront distributions of this account and **denies them `assets/*.m3u8` and
`assets/*.mpd`**: content manifests are for Channel Assembly only, so an asset cannot simply be played from the CDN by
its VOD manifest (verified: 403). Segments, ad manifests under `ads/`, VAST pods and EPG feeds stay readable. This
stops casual direct playback, not a determined viewer: content segments have predictable names
(`assets/<asset>/<ladder>/main_<rendition>_<n>.cmfv`, and every SSAI playlist shows the prefix), so an ad-free
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

- **`media`** (custom, routes 1–3 and 7): `Access-Control-Allow-Origin: *` *overriding* the origin's value,
  `Access-Control-Allow-Headers: *` (CMCD header mode preflights), `Timing-Allow-Origin: *` and `Server-Timing` on
  every response. MediaTailor echoes the request's `Origin` with credentials, which a shared cache must never replay
  to other sites, hence the override on cached MediaTailor routes. `Server-Timing` carries `cdn-rid`, the same value as
  the `x-edge-request-id` log field, so a player can quote the request id of a failed segment.
- **`preflight`** (managed CORS-with-preflight-and-security-headers, routes 4–6): uncached MediaTailor responses keep
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
- the host of the VAST pods, ad beacons and SGAI-ready ad playlists.

Players therefore only ever receive URLs that are relative or on the content domain (plus the MediaTailor exceptions
below), and moving that domain's DNS to another CDN moves all segment traffic without touching MediaTailor. The ladder
kits' fingerprint includes the content host, so changing `content_domain` re-encodes every kit on the next deploy
(minutes, a few dollars per ladder) and gives the house ads new creative ids.

## Multi-CDN in v3.0

What works:

- **Segments on several CDNs** by DNS or traffic steering of `delivery.content_domain`, with each CDN configured from
  the path contract and chaining to CloudFront for the `media` origin.
- **Manifests through another CDN**: a CDN that proxies the MediaTailor routes (rules 3–6) to the MediaTailor origins
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
- The Media stack's `Channels` output and the console's players use the primary CDN; the alternatives are listed,
  not steered.
- A CDN-shielded mode (CloudFront between MediaTailor and Channel Assembly) needs a public channel policy, so it is not
  offered ([ADR-0007](adr/0007-sigv4-channel-origin-no-cdn-shield.md)).

## Requests that bypass the CDN

- **SGAI asset lists** are absolute URLs on `interstitials.mediatailor.<region>.amazonaws.com`: players call
  MediaTailor directly, bypassing your CDN and any WAF in front of it. The same path also works through CloudFront's
  `/v1/*` route, but MediaTailor does not write CDN-relative asset-list URLs and has no setting for it.
- **SGAI server-side beacon segments** go to the playback configuration's own endpoint, then 301 to the CDN (not used
  with preconditioned ads, which report client-side).
- **DASH ad-period segments** (server-side beacons) use an absolute `BaseURL` on
  `manifests.mediatailor.<region>.amazonaws.com/v1/dashsegment/…`, then 301 to the CDN.

## Capacity

Segment delivery scales with CloudFront. The limit to plan for is Channel Assembly's **50 manifest requests per second
per channel** (400 per account), because MediaTailor SSAI fetches the channel manifest about once per player request:
in the end-to-end tests Channel Assembly's `RequestCount` was 0.86 times SSAI's `Requests`.

| | Per SSAI viewer | Channel quota (50 TPS = 3,000 per minute) | Account quota (400 TPS) |
|---|---|---|---|
| Requests | 10–20 per minute (video and one or two audio media playlists every 6 s target duration, plus the multivariant) | about **150–300 concurrent SSAI viewers** | about 1,200–2,400 across all channels |

These figures are extrapolated from measured ratios, not load-tested. To serve more:

1. **Raise the quota before launch.** Request an increase of the MediaTailor "Channel manifest requests" quotas in
   Service Quotas. The `<channel>-manifest-requests` alarm fires when a channel stays at 80 % of 50 TPS
   (2,400 requests per minute) for three minutes; set `channel_manifest_tps_quota` on `MediaObservability` to your
   new quota so the alarm follows it.
2. **Prefer SGAI for capable players.** SGAI media playlists are the same for every session and CloudFront caches them,
   so Channel Assembly load should be bounded per variant and per edge location rather than per viewer. This is
   expected from the protocol but has not been measured at scale.
3. **Fewer playlists per viewer**: fewer audio groups per output, or longer segments in a custom ladder.
