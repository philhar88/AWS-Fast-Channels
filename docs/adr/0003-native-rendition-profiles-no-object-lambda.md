# 0003 — Rendition profiles via MediaConvert AdditionalManifests, no S3 Object Lambda

**Status:** Accepted

## Context
Different channels and devices want different subsets of one encode: AVC only for legacy devices, AV1 for capable TVs,
SD for mobile, everything for Apple players. The original idea was to filter manifests per request with S3 Object
Lambda. S3 Object Lambda has been closed to new customers since 7 November 2025, so a reference architecture cannot
depend on it.

## Decision
A *rendition profile* is a declarative filter (format, codecs, height range) in `fast_channels.core.profiles`. Every
encode writes, through MediaConvert `CmafGroupSettings.AdditionalManifests`, one multivariant playlist and one DASH MPD per
applicable profile next to the shared CMAF segments: `main` (all codecs, HLS), `main_avc`, `main_hevc`, `main_av1`,
`main_sd`, `main_hd`, `main_uhd`. Channel Assembly registers each as an `HttpPackageConfiguration` (one source group per
profile); a channel's outputs choose which groups to publish.

## Consequences
- Zero extra encode cost and no per-request compute; manifests are static and CDN-cacheable.
- The combined DASH MPD is never exposed (MediaConvert puts every codec in one AdaptationSet); DASH is per codec.
- Channel Assembly allows five package configurations per VOD source and five outputs per channel by default (both
  adjustable). The default set is `hls`, `hls-avc`, `hls-hevc`, `hls-av1`, `dash-avc`; enabling more needs a quota increase.
- A profile added after content was encoded needs a re-encode (or a manifest-only repackaging step, a documented
  extension).
