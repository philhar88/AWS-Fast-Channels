# 0013 — Captions are on the roadmap, not in v3.0

**Status:** Superseded by [0014](0014-captions-as-ladder-tracks.md), which [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) superseded in turn

## Context
Production FAST distribution usually requires captions. Channel Assembly needs caption tracks to be identical across
content, slates and ads, so captions change every ladder kit.

## Decision
v3.0 would ship without caption renditions until an end-to-end test proved them.

## Consequences
[0014](0014-captions-as-ladder-tracks.md) added IMSC1 caption tracks; [0016](0016-plain-hls-ts-avc-aac-basic-tier.md)
removed them again with the move to plain HLS MPEG-TS. Captions now wait for a future decision.
