# 0006 — One CMAF encode per asset; the ladder version is the unit of compatibility

**Status:** Superseded by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md)

## Context
Channel Assembly stitches VOD sources by variant position: every program, filler slate and ad slate in a source group
must have the same renditions, or HLS media-sequence numbering breaks at program boundaries. MediaTailor preconditioned
ads must match too.

## Decision
Every asset, slate and creative was encoded once per ladder version into one CMAF output group of AV1, HEVC and AVC
with AAC (HLS and DASH from shared segments). Channels bound to one ladder version, and tools refused assets of
another (`LADDER_MISMATCH`).

## Consequences
[0016](0016-plain-hls-ts-avc-aac-basic-tier.md) keeps the ladder version as the unit of compatibility and replaces the
CMAF encode with one HLS MPEG-TS encode of AVC with muxed AAC on the MediaConvert Basic tier.
