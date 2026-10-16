# 0005 — Preconditioned ads (no MediaTailor transcoding) by default

**Status:** Accepted

## Context
MediaTailor normally transcodes ad creatives to match the content. With a multi-codec AV1 + HEVC + AVC ladder, live tests
showed transcoded ads truncated in AVC/HEVC variants (8 s of video against 10.1 s of audio, padded with slate), AV1 ads
downgraded to 8-bit, HEVC ads to Main profile, and one HE-AAC rendition stitched into both audio groups. Ads
*preconditioned* with the same MediaConvert job template as the content, with `AdConditioningConfiguration` set to
`NONE`, filled every variant exactly in both SSAI and SGAI sessions.

## Decision
- Each ladder version has a *kit*: filler slate, ad-break slates, house-ad creatives encoded with that ladder, and a static
  VAST 4.2 pod per ladder in S3 behind CloudFront. Playback configurations use `AdConditioning NONE` and no `SlateAdUrl`
  (a slate MP4 would be transcoded, reintroducing the defect); under-filled breaks show the Channel Assembly slate.
- SGAI copies of ad media playlists use absolute segment URLs (MediaTailor re-hosts the multivariant under
  `/preconditioned-ads/`); CloudFront routes `/preconditioned-ads/*` to MediaTailor's segment origin.
- `ads.mode = external_vast` supports third-party decision servers with transcoding, restricted to single-codec outputs.

## Consequences
Your own ad server must return creatives encoded for the channel's ladder (or use the external mode). The first break
that sees a new creative shows slate while MediaTailor imports it.
