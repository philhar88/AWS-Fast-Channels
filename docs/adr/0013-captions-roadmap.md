# 0013 — Captions are on the roadmap, not in v3.0

**Status:** Superseded by [0014](0014-captions-as-ladder-tracks.md)

## Context
Production FAST distribution usually requires captions (CEA-608/708 or WebVTT). Channel Assembly needs caption tracks to be
identical across content, slates and ads, so captions change every ladder kit. The v3 research did not validate captions
through Channel Assembly and MediaTailor.

## Decision
v3.0 ships without caption renditions. The ladder model is designed to gain `CaptionRendition` entries (WebVTT sidecar in
the CMAF group, empty tracks for slates and ads) once an end-to-end test proves them.

## Consequences
Adopters who need captions today add a caption selector and WebVTT output to the ladder compiler and re-provision kits.
