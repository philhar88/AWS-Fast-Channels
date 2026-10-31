# Ad insertion: SSAI and preconditioned ads

Every channel has one MediaTailor playback configuration for server-side ad insertion (SSAI): MediaTailor stitches
the ads into a personalised manifest per viewing session, which every HLS player plays. By default the ads are
*preconditioned*: encoded on the channel's own ladder, so MediaTailor never transcodes them. Content and ads are plain
HLS: MPEG-TS segments with AVC video and AAC audio muxed into every variant ([encoding.md](encoding.md)).

- [SSAI sessions](#ssai-sessions)
- [The playback configuration](#the-playback-configuration)
- [Preconditioned ads](#preconditioned-ads)
- [External VAST mode](#external-vast-mode)
- [The ad decision server URL](#the-ad-decision-server-url)
- [Beaconing and ad tracking](#beaconing-and-ad-tracking)
- [Monitoring and troubleshooting](#monitoring-and-troubleshooting)

## SSAI sessions

| | SSAI (stitched) |
|---|---|
| Request | `<CdnBaseUrl>/v1/master/<hash>/<channel>/<output>.m3u8` |
| Manifests | Personalised per session (`/v1/manifest/…`); never cached |
| Ads | Stitched into the media playlist behind discontinuities, at Channel Assembly's `EXT-X-DATERANGE` break markers |
| Ad segments | `/v1/segment/…` URLs in the session's playlist; MediaTailor answers each with a 301 to the segment on the CDN |
| Beacons | Server-side by MediaTailor (implicit sessions) |
| Players | Any HLS player |
| Channel Assembly load | Shared across sessions: about 9 requests per second per channel at 1,600 viewers ([capacity](delivery-and-multi-cdn.md#capacity)) |

Get the URLs from the Media stack's `Channels` output, from `channels___get_channel` (one SSAI URL per output), or
from the console's Channels view, which plays the channel in an SSAI player.

## The playback configuration

`core.specs.playback_configuration` builds it for IaC channels (CloudFormation) and agent channels (API) alike. One
per channel, named after the channel. A channel the release module monetizes also gets sibling configurations
`<channel>-p-*`, created by the Release Controller ([release-controller.md](release-controller.md)).

| Setting | Value | Why |
|---|---|---|
| `VideoContentSourceUrl` | The channel's Channel Assembly endpoint `https://<id>.channel-assembly.mediatailor.<region>.amazonaws.com/v1/channel/<channel>/` | MediaTailor signs requests to Channel Assembly with SigV4 |
| `InsertionMode` | `STITCHED_ONLY` | Server-side ad insertion only |
| `AdDecisionServerUrl` | The ladder's VAST pod with correlation parameters (preconditioned), or `ads.url` (external) | [below](#the-ad-decision-server-url) |
| `CdnConfiguration` | `AdSegmentUrlPrefix` and `ContentSegmentUrlPrefix` = the content base URL | Ad segments go through the CDN |
| `AdConditioningConfiguration.StreamingMediaFileConditioning` | `NONE` | Streaming creatives are stitched as they are |
| `PersonalizationThresholdSeconds` | 5 | |
| `AdsPersonalizationTimeouts` | ADS request 2000 ms; live and VOD personalisation 6000 ms | All three must be set together or the API rejects the configuration |
| `LogConfiguration` | `VENDED_LOGS` for `observability.mediatailor_log_percent` of sessions (default 100; use about 10 in production); raw ADS responses included, generated manifests excluded | One shared log group per deployment |
| `SlateAdUrl` | not set | MediaTailor would transcode it; an unfilled avail shows the Channel Assembly break slate, which is already on the ladder |

## Preconditioned ads

### Why

MediaTailor normally transcodes creatives to match the content. With the multi-codec AV1 + HEVC + AVC ladder of the
first v3.0.0 encode, the live tests showed transcoded ads truncated in AVC and HEVC variants (8 s of video against
10.1 s of audio, padded with slate), AV1 ads downgraded to 8-bit, HEVC ads to Main profile, and one HE-AAC rendition
stitched into both audio groups. Creatives encoded with the content's own job template and stitched with conditioning
`NONE` filled every variant exactly ([ADR-0005](adr/0005-preconditioned-ads.md)). The AVC + AAC ladder keeps that
default: a preconditioned ad is stitched as it was encoded, so every variant gets exactly that ad and no transcode is
billed.

MediaTailor stitches a preconditioned ad only when it matches the content's variants: the same resolution and codec,
and a bitrate within 8 kbit/s after rounding. House ads are encoded with the channel ladder's own job template, so
they are MPEG-TS variants of the same rungs, in the same order, with the same muxed AAC track.

### How

1. Each ladder version's [kit](encoding.md#ladder-kits) includes two house ads of 15 GOPs (30.03 s each), encoded
   with the ladder's job template.
2. Finalize writes a **copy of each ad's manifests** under `ads/<ladder>/<creative_id>/`: the multivariant with its
   child URIs kept relative, and media playlists whose segment URIs are absolute on the content host, pointing at the
   ad's segments under `assets/`. The CDN never serves `assets/*.m3u8`
   ([delivery](delivery-and-multi-cdn.md#origins-and-access-control)), so MediaTailor reads the ad's manifests from
   the copy, through the CDN.
3. The kit writes a static **VAST 4.2 pod** to `ads/<ladder>/vast.xml`, repeating the house ads until the pod covers
   the longest break slate, so every break length is a whole number of ads. Each `<Ad>` has one streaming
   `MediaFile`: the HLS multivariant of the copy. There is deliberately no progressive MP4: with conditioning `NONE`,
   MediaTailor would transcode one when it cannot use the streaming file.
4. Creative ids are `<creative>-<ladder key>-<host hash>` and appear as `Creative@id` and `UniversalAdId`. MediaTailor
   caches imports by creative, so ids are never reused for different media or URLs; a new content host produces new
   ids.

MediaTailor stitches the pod's ads that fit the avail into each session's media playlist as `/v1/segment/…` URLs,
which it answers with a 301 to the ad segment on the CDN; the rest of the break shows the break slate. The first break
that meets a new creative is skipped (`NEW_CREATIVE`, then `TRANSCODE_IN_PROGRESS` in the logs) while MediaTailor
imports it; later breaks fill.

### Your own ad server with preconditioned creatives

To keep the preconditioned path with real campaigns, encode your creatives with the channel's ladder (ingest them with
`kind: "ad"`, see [integrate-your-cms.md](integrate-your-cms.md#ingest-contract)). They then have the channel's exact
MPEG-TS variants. The library asset carries the creative id and the absolute `hls_uri` of its manifest copy. Have
your ADS return VAST with that streaming `MediaFile` (and no progressive file), versioning `Creative@id` and
`UniversalAdId` per ladder version, and configure its URL with `ads.mode: external_vast` (next section).

## External VAST mode

```json
"ads": {
  "mode": "external_vast",
  "url": "https://ads.example.com/vast?sid=[session.uuid]&dur=[session.avail_duration_secs]&upid=[scte.segmentation_upid]"
}
```

The playback configurations then call your ad decision server. Caveats:

- Creatives that are not preconditioned are transcoded by MediaTailor from their progressive files to match the
  content's MPEG-TS variants, and served under `/tm/*` (routed by CloudFront to MediaTailor's segment origin).
- The first break that meets a new creative is skipped while it is transcoded.
- MediaTailor includes 10 transcodes per 1,000 ad insertions; beyond that they are billed at MediaConvert rates.
- The URL is validated against the documented MediaTailor variables; an unknown variable would silently become an
  empty string.

## The ad decision server URL

The default (preconditioned) ADS URL of a channel is its ladder's pod on the content host:

```
<ContentBaseUrl>/ads/<ladder key>/vast.xml?ch=<channel>&sid=[session.uuid]&evid=[scte.event_id]&upid=[scte.segmentation_upid]&dur_ms=[session.avail_duration_ms]
```

The pod is static, so the parameters exist for traceability: the CloudFront log of every ad request records the
channel, the MediaTailor session, the SCTE-35 event id and the UPID of the break, and the exact avail duration
(29.97 fps breaks are not whole seconds). MediaTailor strips the `FST3` format identifier from the MPU UPID, so
`upid` arrives as the content id itself, in plain text: `…&evid=14366456&upid=e2e-pattern&dur_ms=30030` in a live
`MAKING_ADS_REQUEST` (the SCTE-35 descriptor and the library hold the bytes `FST3e2e-pattern`, hex
`465354336532652D7061747465726E`).

Variables accepted in any ADS URL (`core.specs.ADS_VARIABLES`):

| Family | Variables |
|---|---|
| Session | `session.id`, `session.uuid`, `session.avail_duration_secs`, `session.avail_duration_ms`, `session.client_ip`, `session.user_agent`, `session.referer`, `session.accept_language` |
| Avail | `avail.index`, `avail.random`, `avail.source_content_time_epoch_ms` |
| SCTE-35 | `scte.event_id`, `scte.avail_num`, `scte.avails_expected`, `scte.segmentation_event_id`, `scte.segmentation_type_id`, `scte.segmentation_upid`, `scte.segmentation_upid.assetId`, `scte.segmentation_upid.cueData.key`, `scte.segmentation_upid.cueData.value`, `scte.segmentation_upid.private_data.<n>`, `scte.unique_program_id`, `scte.segment_num`, `scte.segments_expected`, `scte.sub_segment_num`, `scte.sub_segments_expected`, `scte.delivery_not_restricted_flag`, `scte.archive_allowed_flag`, `scte.device_restrictions`, `scte.no_regional_blackout_flag` |
| Player | `player_params.<name>`, set by the player as `ads.<name>=<value>` on the session's first request |

## Beaconing and ad tracking

- **SSAI, implicit sessions** (a plain `GET` of the multivariant): MediaTailor fires the VAST impression and quartile
  beacons server-side as the player fetches the ad segments through `/v1/segment/…` redirects (301 to the CDN).
  Never cache those redirects; they are unique per session and expire.
- **SSAI, explicit sessions** (`POST /v1/session/…`): MediaTailor returns a `trackingUrl` for client-side reporting.
- **The kit's beacons** point at `<ContentBaseUrl>/ads/b.gif?evt=<event>&cr=<creative>` (`Cache-Control: no-store`),
  so every impression and quartile appears in the CloudFront access logs. MediaTailor does not expand VAST macros such
  as `[CACHEBUSTING]`; they arrive literally.

## Monitoring and troubleshooting

- **Alarms** (per IaC channel): `<channel>-ads-errors` (ADS errors and timeouts) and `<channel>-fill-rate`
  (`Avail.FillRate` below 0.5 for 15 minutes). See [observability.md](observability.md#dashboard-and-alarms).
- **Vended logs** in `/aws/vendedlogs/mediatailor/<prefix>`: `MAKING_ADS_REQUEST` (the ADS URL with every variable
  resolved), `FILLED_AVAIL` (`avail.fillRate`, `skippedAds[].skippedReason`), `BEACON_FIRED`, and transcode events.

| Symptom | Likely cause | Action |
|---|---|---|
| The first break after a deploy or a new creative shows only slate | `NEW_CREATIVE` import | Expected; later breaks fill. Warm up a demo channel for a few breaks |
| Breaks never fill | ADS URL unreachable, or creatives do not match the ladder's variants | Check `MAKING_ADS_REQUEST` and `FILLED_AVAIL` in the vended logs; `curl` the VAST URL through the CDN |
| SSAI returns 504 for a few minutes after a channel change | A new channel policy takes minutes to propagate; Channel Assembly answers 403 meanwhile | Wait; avoid changing the policy on a live channel |
| HTTP 400 `GUIDED insertion mode not allowed` | The player asked for server-guided insertion (`?aws.insertionMode=GUIDED`); the playback configuration is `STITCHED_ONLY` | Play the SSAI URL without that query string |
| mediastreamvalidator: 552026 *Removed an EXT-X-DATERANGE while mapped to range in playlist*, for most breaks, on every live media playlist: the Channel Assembly origin and SSAI | Channel Assembly drops a break's `DATERANGE` from the first playlist whose first segment starts exactly at `START-DATE` + `DURATION`. The validator counts that instant as part of the range (RFC 8216bis section 6.2.1). It misses a break only when `START-DATE` + `DURATION` rounds just below the in-point in its double-precision dates | Channel Assembly behaviour, not configuration: the tier, the window length, the SCTE-35 message type and MediaTailor do not change it, and AWS documents the other markup, `SCTE35_ENHANCED`, for VOD only. No ad or beacon is lost: the tag goes about one window after the break ends |
| mediastreamvalidator: 536003 *Program date-time tags are too far apart* at the second ad of a pod | In SSAI, MediaTailor writes the `EXT-X-PROGRAM-DATE-TIME` of the first segment of the second and later ads of a pod one segment (about 6 s) off in some reloads, in every rendition, also when the VAST returns exactly the ads that fill the break | MediaTailor behaviour: Channel Assembly's own playlists keep every date |
| `EXT-X-MEDIA-SEQUENCE` one lower than the reload before | MediaTailor sometimes answers a reload with an older session playlist, with or without the CDN; seen by a poller reloading about every second, never by the validator | MediaTailor behaviour: Channel Assembly's own playlists never go back |
