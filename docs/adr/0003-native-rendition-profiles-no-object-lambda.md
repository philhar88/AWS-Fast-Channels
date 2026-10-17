# 0003 — Rendition profiles are static encode manifests, no S3 Object Lambda

**Status:** Accepted, amended by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md)

## Context
Channels and devices may want different subsets of one encode. The original idea was to filter manifests per request
with S3 Object Lambda. S3 Object Lambda has been closed to new customers since 7 November 2025, so a reference
architecture cannot depend on it.

## Decision
A *rendition profile* is a named manifest that the encode itself writes (`fast_channels.core.profiles`). Channel
Assembly registers each profile as an `HttpPackageConfiguration` (one source group per profile), and a channel's
outputs choose which groups to publish. Since [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) there is one profile,
`hls`: the Apple HLS output group's own `main.m3u8` with every variant of the ladder. The first v3.0.0 encode wrote
per-codec and per-resolution HLS and DASH manifests through `CmafGroupSettings.AdditionalManifests`; the AVC-only
ladder has nothing to split.

## Consequences
- No extra encode cost and no per-request compute; manifests are static and CDN-cacheable.
- The catalogue stays a list, so a deployment names the profiles it registers (`package_profiles`) and a channel the
  outputs it serves. Channel Assembly allows five package configurations per VOD source and five outputs per channel
  by default.
- A new profile needs a re-encode (or a manifest-only repackaging step, a documented extension).
