# 0017 — SSAI only; frame-accurate ad cues with ESAM

**Status:** Accepted (amends [0005](0005-preconditioned-ads.md), [0008](0008-ad-break-alignment.md) and
[0016](0016-plain-hls-ts-avc-aac-basic-tier.md))

## Context
[0016](0016-plain-hls-ts-avc-aac-basic-tier.md) kept one `PLAYER_SELECT` playback configuration per channel, serving
SSAI and SGAI (HLS Interstitials), and kept every encode on MediaConvert's Basic tier by snapping cue points to the
6.006 s segment grid, so a break could land up to 3 s away from its editorial cue.

SGAI brought its own machinery and caveats: CloudFront routes for MediaTailor's re-hosted ad playlists
(`/preconditioned-ads/*`) and shared interstitial media playlists (`/v1/i-media/*`), asset lists that players fetch
from `interstitials.mediatailor.<region>.amazonaws.com` around the CDN, client-side beacons
(`aws.reportingMode=CLIENT`), and an hls.js 1.7.3 stall entering an append-in-place interstitial when AAC is muxed
into the content. SSAI plays on every HLS player and carries the channel's ad revenue.

Editors place cue points on scene changes, and a break up to 3 s away cuts into the scene. In the first v3.0.0 encode
ESAM had caused problems on tracks that the MPEG-TS ladder no longer has (multi-period DASH, AV1 and separate audio
honouring only GOP-grid points, caption tracks re-cut). On the TS ladder it splits every track at the cue: in a real
job (2026-10-17), cues at 13.480 s and 26.994 s split all three variants exactly there (segment boundaries
12.012–13.480–18.018 and 24.024–26.994–30.030 s), with the audio muxed in, and the grid resumed after each cue. ESAM
is a Professional-tier feature, and MediaConvert bills every output of a job that uses it at that tier.

## Decision
- **SSAI only.** Playback configurations use `InsertionMode: STITCHED_ONLY`, and `core.specs.playback_urls` returns
  one SSAI URL per output. There are no `?aws.insertionMode=GUIDED` URLs, HLS Interstitials, asset lists,
  `aws.reportingMode=CLIENT` or client-side beacons, and the console has one player per channel. CloudFront drops the
  `/preconditioned-ads/*` and `/v1/i-media/*` routes.
- **Preconditioned house ads stay** (`StreamingMediaFileConditioning NONE`, the kit's static VAST pod). The CDN
  never serves `assets/*.m3u8`, so Finalize still copies each ad's manifests under `ads/<ladder>/<creative_id>/`: the
  multivariant with relative child URIs, the media playlists with absolute segment URIs on the CDN. The VAST
  `MediaFile` is that copy's multivariant, and MediaTailor stitches the ad segments into each SSAI session through
  `/v1/segment` redirects (301) to the CDN.
- Channel Assembly keeps `EXT-X-DATERANGE` ad markup, the markup it writes for live HLS; MediaTailor inserts the ads
  at those markers.
- **Frame-accurate cue points.** Cue points snap to the nearest frame (`Grid.snap_to_frame`, rounded to the
  millisecond). A point on the natural segment grid (`Grid.on_segment_grid`) needs nothing. Any other point makes
  `build_job_overrides` add `Settings.Esam` (`core.esam.build_esam`: an `SccXml` signal processing notification with
  a placement opportunity per point, `ResponseSignalPreroll` 0) and set `JobOverrides.professional`.
- **Professional tier per job.** Only a job with ESAM is billed on the Professional tier: about $0.06 per content
  minute on `standard` instead of $0.0375 (`encode_cost(..., professional=True)`). The job template, the kits and
  titles without off-grid cue points stay on the Basic tier.
- Finalize keeps a cue point as a break offset when a segment boundary that every variant shares lies within 100 ms
  of it (Channel Assembly's rule for ad-break offsets); with ESAM that boundary is the snapped frame. Other points are
  dropped with a warning. Ad breaks use the snapped offsets.

## Consequences
- Breaks land within half a frame of the editorial cue (about ±17 ms at 29.97 fps) instead of within ±3 s.
- A title with off-grid cue points costs 1.6 times as much to encode ($3.60 instead of $2.25 per content hour on
  `standard`); the three sample films are such titles. Editors who put cue points on the segment grid keep the Basic
  rate.
- Each off-grid cue point replaces one segment with two shorter ones in every variant. Channel Assembly reports
  `SEGMENT_DURATION_MISMATCH` warnings for them, which the tools filter.
- Every viewer is an SSAI session, and there are no cacheable SGAI playlists. MediaTailor reuses Channel Assembly
  responses across sessions, so MediaTailor's Ad Insertion requests quota bounds the audience, not Channel Assembly's
  per-channel quota: about 37,000 concurrent viewers per account at 70 % of default quotas. See
  [capacity](../delivery-and-multi-cdn.md#capacity).
- The hls.js caveat with muxed audio no longer applies. The AWS findings at Must level remain: Channel Assembly
  552026 and MediaTailor SSAI 536003.
