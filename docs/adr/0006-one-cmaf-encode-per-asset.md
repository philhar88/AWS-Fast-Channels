# 0006 — One CMAF encode per asset; the ladder version is the unit of compatibility

**Status:** Accepted

## Context
Channel Assembly stitches VOD sources by variant position: every program, filler slate and ad slate in a source group
must have the same renditions, or HLS media-sequence numbering breaks at program boundaries. MediaTailor preconditioned
ads must match too.

## Decision
- A ladder is an immutable, fingerprinted model (`standard@86bf8088e0`). Every asset, slate and creative is encoded once
  per ladder version into one CMAF output group (HLS + DASH from shared segments).
- Channels bind to one ladder version. Tools refuse to schedule assets from another ladder (`LADDER_MISMATCH`).
- Changing a ladder means provisioning a new version (job template + kit) and re-encoding the titles a channel needs.
- Defaults: `standard` tops out at 1080p (AVC 8, HEVC 4, AV1 4 rungs, AAC 3; ~$0.56 per content minute). `uhd` adds
  1440p/2160p HEVC Main10 and AV1 10-bit (~$1.36 per minute).

## Consequences
Per-title encoding and MediaConvert automated ABR are not used (Channel Assembly does not support varying ladders).
