# 0014 — Captions are IMSC1 tracks of the ladder, sourced provided, then embedded, then generated

**Status:** Superseded by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) (it superseded [0013](0013-captions-roadmap.md))

## Context
FAST outlets and US IP-captioning rules expect closed captions. Channel Assembly matches subtitle renditions by
position, so every program, slate and ad of a channel needs the same caption tracks.

## Decision
A ladder declared IMSC1 caption tracks in its CMAF group, and every asset carried them, empty where it had none. Each
track came from a provided file, else from CEA-608/708 in the source, else from a captioner
([0015](0015-pluggable-captioner-elemental-inference.md)).

## Consequences
The tracks brought MediaTailor and hls.js behaviour that the pipeline had to work around: blank WebVTT in the subtitle
rendition during SSAI ads, caption segments re-cut on the video's after ESAM splits, and hls.js subtitle stalls.
[0016](0016-plain-hls-ts-avc-aac-basic-tier.md) removed captions with the move to plain HLS MPEG-TS on the Basic tier.
