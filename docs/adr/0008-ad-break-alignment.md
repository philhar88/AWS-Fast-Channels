# 0008 — Ad breaks on segment boundaries; cue points snapped to the segment grid

**Status:** Accepted, amended by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md)

## Context
Channel Assembly inserts ad breaks only at segment boundaries common to every track (within 100 ms). MediaConvert ESAM
can force extra boundaries, but it makes jobs Professional tier, shortens segments (flooding Channel Assembly with
`SEGMENT_DURATION_MISMATCH` alerts) and, in the first v3.0.0 CMAF encode, made DASH multi-period and was honoured by AV1
and audio tracks only on the GOP grid. That encode therefore snapped cue points to the GOP grid and added ESAM only for
points between segment boundaries.

## Decision
Since [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) every encode stays on the Basic tier, which has no ESAM. Cue
points from the CMS (or the v2-compatible `AdOffsets` tag) are snapped to the nearest boundary of the 6.006 s segment
grid (`Grid.snap_to_segment`). Audio is muxed into every segment, so each segment boundary is a boundary of every track.
The library stores every asset's valid break offsets; the scheduler picks from them and emits SCTE-35 TIME_SIGNAL
breaks (segmentation type 0x34, MPU UPID carrying the content id).

## Consequences
Breaks land within ±3 s (half a segment) of the editorial cue. No segment is ever split, so every segment but a title's
last is 6.006 s. Editors who need frame-accurate breaks place cue points on the segment grid.
