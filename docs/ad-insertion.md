# Ad insertion: SSAI, SGAI and preconditioned ads

Every channel has one MediaTailor playback configuration that serves both server-side ad insertion (SSAI, stitched
manifests) and server-guided ad insertion (SGAI, HLS Interstitials). By default the ads are *preconditioned*:
encoded on the channel's own ladder, so MediaTailor never transcodes them.

- [SSAI or SGAI](#ssai-or-sgai)
- [The playback configuration](#the-playback-configuration)
- [Preconditioned ads](#preconditioned-ads)
- [External VAST mode](#external-vast-mode)
- [The ad decision server URL](#the-ad-decision-server-url)
- [Beaconing and ad tracking](#beaconing-and-ad-tracking)
- [DASH](#dash)
- [Monitoring and troubleshooting](#monitoring-and-troubleshooting)

## SSAI or SGAI

The playback configuration uses `InsertionMode: PLAYER_SELECT`, so each session chooses.

| | SSAI (stitched) | SGAI (HLS Interstitials) |
|---|---|---|
| Request | `<CdnBaseUrl>/v1/master/<hash>/<channel>/<output>.m3u8` | the same URL with `?aws.insertionMode=GUIDED` |
| Formats | HLS and DASH (`/v1/dash/<hash>/<channel>/<output>.mpd`) | HLS only |
| Manifests | Personalised per session; never cached | Media playlists (`/v1/i-media/…`) are identical for every session and cached by CloudFront for the origin's 1 s |
| Ads | Stitched into the media playlist, behind discontinuities | `EXT-X-DATERANGE` interstitial; the player fetches the break's asset list |
| Asset list | n/a | Absolute URL on `interstitials.mediatailor.<region>.amazonaws.com`: players call MediaTailor directly, not through the CDN |
| Beacons | Server-side by MediaTailor (implicit sessions) | Client-side: add `aws.reportingMode=CLIENT` and fire the asset list's `TRACKING` beacons |
| Players | Any HLS or DASH player | Apple platforms 16.4+, hls.js 1.6+ (the console uses 1.7.3), other players with HLS Interstitials support |
| Channel Assembly load | About one origin request per session request | Expected to be bounded per variant by the CDN cache (not measured at scale) |

Get the URLs from the Media stack's `Channels` output, from `channels___get_channel`, or from the console's Channels
view, which plays an SSAI and an SGAI session of the same output side by side. SGAI with DASH returns HTTP 500.

## The playback configuration

`core.specs.playback_configuration` builds it for IaC channels (CloudFormation) and agent channels (API) alike. One
per channel, named after the channel.

| Setting | Value | Why |
|---|---|---|
| `VideoContentSourceUrl` | The channel's Channel Assembly endpoint `https://<id>.channel-assembly.mediatailor.<region>.amazonaws.com/v1/channel/<channel>/` | MediaTailor signs requests to Channel Assembly with SigV4 |
| `InsertionMode` | `PLAYER_SELECT` | SSAI and SGAI from one configuration |
| `AdDecisionServerUrl` | The ladder's VAST pod with correlation parameters (preconditioned), or `ads.url` (external) | [below](#the-ad-decision-server-url) |
| `CdnConfiguration` | `AdSegmentUrlPrefix` and `ContentSegmentUrlPrefix` = the content base URL | Ad segments and re-hosted ad playlists go through the CDN |
| `AdConditioningConfiguration.StreamingMediaFileConditioning` | `NONE` | Streaming creatives are stitched as they are |
| `DashConfiguration` | `MpdLocation: DISABLED`, `OriginManifestType: MULTI_PERIOD` | Keeps DASH players on the CDN instead of a `Location` pointing at MediaTailor |
| `PersonalizationThresholdSeconds` | 5 | |
| `AdsPersonalizationTimeouts` | ADS request 2000 ms; live and VOD personalisation 6000 ms | All three must be set together or the API rejects the configuration |
| `LogConfiguration` | `VENDED_LOGS` for `observability.mediatailor_log_percent` of sessions (default 100; use about 10 in production); raw ADS responses included, generated manifests excluded | One shared log group per deployment |
| `SlateAdUrl` | not set | MediaTailor would transcode it; an unfilled avail shows the Channel Assembly break slate, which is already on the ladder |

## Preconditioned ads

### Why

MediaTailor normally transcodes creatives to match the content. With a multi-codec AV1 + HEVC + AVC ladder, the live
tests showed transcoded ads truncated in AVC and HEVC variants (8 s of video against 10.1 s of audio, padded with
slate), AV1 ads downgraded to 8-bit, HEVC ads to Main profile, and one HE-AAC rendition stitched into both audio
groups. Creatives encoded with the content's own job template and stitched with conditioning `NONE` filled every
variant exactly, in SSAI and SGAI sessions ([ADR-0005](adr/0005-preconditioned-ads.md)).

### How

1. Each ladder version's [kit](encoding.md#ladder-kits) includes two house ads of 15 GOPs (30.03 s each), encoded
   with the ladder's job template, its caption tracks included (empty).
2. Finalize writes an **SGAI-ready copy** of each ad's manifests under `ads/<ladder>/<creative_id>/`: media playlists
   with absolute segment and `EXT-X-MAP` URIs on the content host, multivariant child URIs kept relative, and DASH
   MPDs with absolute segment templates. MediaTailor re-hosts only the multivariant playlist of a preconditioned ad
   (under `/preconditioned-ads/` on the ad segment prefix), so relative segment URIs would resolve against
   MediaTailor's host and fail; for DASH it resolves the templates against the copy's location and ignores `BaseURL`
   (v3.0.0 wrote a `BaseURL`, and DASH ad segments then redirected to init segments that did not exist).
   CloudFront routes `/preconditioned-ads/*` to MediaTailor's segment origin.
3. The kit writes a static **VAST 4.2 pod** to `ads/<ladder>/vast.xml`, repeating the house ads until the pod covers
   the longest break slate, so every break length is a whole number of ads. Each `<Ad>` has two streaming `MediaFile`s:
   the HLS multivariant of the SGAI-ready copy and the per-codec DASH MPD. There is deliberately no progressive MP4:
   with conditioning `NONE`, MediaTailor would transcode one when it cannot use the streaming files, which is the
   path that failed.
4. Creative ids are `<creative>-<ladder key>-<host hash>` and appear as `Creative@id` and `UniversalAdId`. MediaTailor
   caches imports by creative, so ids are never reused for different media or URLs; a new content host produces new
   ids.

MediaTailor stitches the pod's ads that fit the avail; the rest of the break shows the break slate. The first break
that meets a new creative is skipped (`NEW_CREATIVE`, then `TRANSCODE_IN_PROGRESS` in the logs) while MediaTailor
imports it; later breaks fill.

### Your own ad server with preconditioned creatives

To keep the preconditioned path with real campaigns, encode your creatives with the channel's ladder (ingest them with
`kind: "ad"`, see [integrate-your-cms.md](integrate-your-cms.md#ingest-contract)). The library asset then carries the
creative id and the absolute `hls_uri` and `dash_uri` of its SGAI-ready copy. Have your ADS return VAST with those two
streaming `MediaFile`s (and no progressive file), versioning `Creative@id` and `UniversalAdId` per ladder version, and
configure its URL with `ads.mode: external_vast` (next section).

## External VAST mode

```json
"ads": {
  "mode": "external_vast",
  "url": "https://ads.example.com/vast?sid=[session.uuid]&dur=[session.avail_duration_secs]&upid=[scte.segmentation_upid]"
}
```

The playback configurations then call your ad decision server. Caveats:

- Creatives that are not preconditioned are transcoded by MediaTailor from their progressive files and served under
  `/tm/*` (routed by CloudFront to MediaTailor's segment origin). Transcoding produces the defects described above
  when one manifest mixes codecs, so **every channel output must be single-codec** in this mode: `hls-avc`,
  `hls-hevc`, `hls-av1` or a `dash-*` output. Configuration validation rejects `hls`.
- The first break that meets a new creative is skipped while it is transcoded.
- MediaTailor includes 10 transcodes per 1,000 ad insertions; beyond that they are billed at MediaConvert rates.
- The URL is validated against the documented MediaTailor variables; an unknown variable would silently become an
  empty string.
- Client-side beacons of SGAI sessions go to your tracking hosts. The operator console's Content-Security-Policy does
  not list third-party hosts, so its beacon emitter cannot reach them; your production players are not affected.

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
  beacons server-side as the player fetches the ad video segments through `/v1/segment/…` redirects (301 to the CDN).
  Never cache those redirects; they are unique per session and expire. With preconditioned ads only the video goes
  through them: the ad audio is served straight from the creative's own CMAF output, `/assets/<ad asset>/…` on the
  content host, on the CDN's default route. In CDN logs, count ad audio by the house ads' asset ids under
  `/assets/`, not by the redirect route.
- **SSAI, explicit sessions** (`POST /v1/session/…`): MediaTailor returns a `trackingUrl` for client-side reporting.
- **SGAI**: request `?aws.insertionMode=GUIDED&aws.reportingMode=CLIENT`. Each asset list then contains a `TRACKING`
  block with the beacon URLs and their times, and the player fires them. Server-side beaconing is not possible for
  preconditioned ads in SGAI, because MediaTailor only re-hosts their manifests. The console implements a client-side
  emitter (`console/src/players/beacons.ts`).
- **The kit's beacons** point at `<ContentBaseUrl>/ads/b.gif?evt=<event>&cr=<creative>` (`Cache-Control: no-store`),
  so every impression and quartile appears in the CloudFront access logs. MediaTailor does not expand VAST macros such
  as `[CACHEBUSTING]`; they arrive literally.

## DASH

DASH is SSAI only. Channel Assembly outputs a multi-period MPD (60 s window) per DASH output; ad periods are stitched
from the per-codec ad MPDs. DASH ad-segment requests used for server-side beacons go directly to
`manifests.mediatailor.<region>.amazonaws.com/v1/dashsegment/…` (MediaTailor writes that absolute `BaseURL` even
behind a CDN) and end in a 301 to the CDN. The default package profiles include `dash-avc`; add `dash-hevc` or
`dash-av1` if your DASH devices need them.

## Captions during ads

House ads carry the ladder's caption tracks (empty), so every stream keeps one rendition set through a break. During
an SSAI HLS ad MediaTailor serves one blank WebVTT (`/v1/webvtt/…/web.vtt`) in every subtitle rendition; SGAI
interstitials show the ad's own (empty) tracks while the primary keeps the slate's; DASH ad periods carry the ad's
IMSC AdaptationSet. Your own preconditioned creatives must be encoded with the channel's ladder, caption tracks
included. Details in [captions.md](captions.md#through-channel-assembly-ad-insertion-and-players).

## Monitoring and troubleshooting

- **Alarms** (per IaC channel): `<channel>-ads-errors` (ADS errors and timeouts) and `<channel>-fill-rate`
  (`Avail.FillRate` below 0.5 for 15 minutes). See [observability.md](observability.md#dashboard-and-alarms).
- **Vended logs** in `/aws/vendedlogs/mediatailor/<prefix>`: `MAKING_ADS_REQUEST` (the ADS URL with every variable
  resolved), `FILLED_AVAIL` (`avail.fillRate`, `skippedAds[].skippedReason`), `BEACON_FIRED`, and transcode events.

| Symptom | Likely cause | Action |
|---|---|---|
| The first break after a deploy or a new creative shows only slate | `NEW_CREATIVE` import | Expected; later breaks fill. Warm up a demo channel for a few breaks |
| Breaks never fill | ADS URL unreachable, or creatives do not match the ladder | Check `MAKING_ADS_REQUEST` and `FILLED_AVAIL` in the vended logs; `curl` the VAST URL through the CDN |
| SGAI plays content but no ads | Asset list fetch blocked, or player without HLS Interstitials | Check the player's network log for `interstitials.mediatailor…`; use hls.js 1.6+ or AVPlayer |
| SGAI in hls.js stalls at a break | hls.js 1.7.3 enters an append-in-place interstitial only when the audio and video buffers reach within 10 ms of the `DATERANGE` start | Seen with AAC framing in local tests; verify with your player version |
| SSAI returns 504 for a few minutes after a channel change | A new channel policy takes minutes to propagate; Channel Assembly answers 403 meanwhile | Wait; avoid changing the policy on a live channel |
| HTTP 400 `GUIDED insertion mode not allowed` | The playback configuration is `STITCHED_ONLY` | The constructs always set `PLAYER_SELECT`; check for manual edits |
