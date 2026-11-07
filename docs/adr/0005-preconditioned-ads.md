# 0005 — Preconditioned ads (no MediaTailor transcoding) by default

**Status:** Accepted, amended by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md),
[0017](0017-ssai-only-frame-accurate-cues-with-esam.md) and the creatives module (2026-11-07, below)

## Context
MediaTailor normally transcodes ad creatives to match the content. With the first v3.0.0 ladder (AV1 + HEVC + AVC
in CMAF), live tests showed transcoded ads truncated in AVC/HEVC variants (8 s of video against 10.1 s of audio,
padded with slate), AV1 ads downgraded to 8-bit, HEVC ads to Main profile, and one HE-AAC rendition stitched into both
audio groups. Ads *preconditioned* with the same MediaConvert job template as the content, with
`AdConditioningConfiguration` set to `NONE`, filled every variant exactly in both SSAI and SGAI sessions. MediaTailor
stitches a preconditioned ad only when it matches the content's variants (bitrate, resolution and codec), which an ad
encoded on the content's own ladder does by construction.

## Decision
- Each ladder version has a *kit*: filler slate, ad-break slates, house-ad creatives encoded with that ladder, and a static
  VAST 4.2 pod per ladder in S3 behind CloudFront. Playback configurations use `AdConditioning NONE` and no `SlateAdUrl`
  (a slate MP4 would be transcoded); under-filled breaks show the Channel Assembly slate.
- Finalize copies each ad's manifests under `ads/<ladder>/<creative_id>/`, the media playlists with absolute segment
  URLs on the CDN, because the CDN does not serve `assets/*.m3u8`; the pod's `MediaFile` is the copy's multivariant.
  Since [0017](0017-ssai-only-frame-accurate-cues-with-esam.md) channels are SSAI only: MediaTailor stitches the ads
  through `/v1/segment` redirects, and the SGAI route `/preconditioned-ads/*` is gone.
- `ads.mode = external_vast` supports third-party decision servers, whose creatives MediaTailor transcodes.
- Since [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) the ladder is AVC with muxed AAC in MPEG-TS, so house ads are
  TS variants of the same three rungs, and the pod's creatives have one streaming `MediaFile` (HLS).

## Consequences
Your own ad server must return creatives encoded for the channel's ladder (or use the external mode). The first break
that sees a new creative shows slate while MediaTailor imports it.

## Amendment (2026-11-07): advertisers' own creatives, preconditioned the same way

The optional creatives module ([creatives.md](../creatives.md)) extends the kit's rule to advertisers' creatives:

- **Conditioning.** A registered creative is encoded by the same ingest pipeline and job template as the content and
  the kit (`kind: ad` on the default ladder), so it matches the ladder by construction, and Finalize writes its
  `ads/<ladder>/<creative_id>/` copy as for a house ad. Its source waits in a quarantine bucket of its own, never in
  the ingest bucket, whose upload rule ingests any video as content.
- **Conformance.** Before the own book may bid a creative, a check confirms what MediaTailor does not: variant count,
  resolution, codec and `BANDWIDTH` against the ladder, and a duration on the 30.03 s grid. MediaTailor stitches an
  off-ladder streaming file by nearest match and never falls back to its progressive file.
- **First sight.** A first sight still costs the whole synchronized break, so a registered creative that is not yet
  WARM wins its slot only where a standalone backs it (ad buffet: a standalone replaced every first-sight-failed ad
  in its slot). On prefetched configurations the import runs at retrieval, before the break.
- **Truncation.** The transcode truncation that motivated this ADR is a CMAF multi-codec effect: on the TS AVC ladder
  of [0016](0016-plain-hls-ts-avc-aac-basic-tier.md), transcoded 30.03 s and 15.015 s ads kept their full video
  duration in every rendition.
