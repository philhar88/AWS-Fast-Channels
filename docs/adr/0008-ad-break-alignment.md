# 0008 — Ad breaks on segment boundaries; frame-accurate cue points, ESAM when off the segment grid

**Status:** Accepted, amended by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) and
[0017](0017-ssai-only-frame-accurate-cues-with-esam.md)

## Context
Channel Assembly inserts ad breaks only at segment boundaries common to every track (within 100 ms). MediaConvert ESAM
can force extra boundaries, but it makes jobs Professional tier, shortens segments (Channel Assembly then reports
`SEGMENT_DURATION_MISMATCH` warnings) and, in the first v3.0.0 CMAF encode, made DASH multi-period and was honoured by
AV1 and audio tracks only on the GOP grid. That encode therefore snapped cue points to the GOP grid and added ESAM only
for points between segment boundaries. [0016](0016-plain-hls-ts-avc-aac-basic-tier.md) then kept every encode on the
Basic tier, without ESAM, and snapped cue points to the 6.006 s segment grid, so breaks landed within ±3 s of the cue.

## Decision
Since [0017](0017-ssai-only-frame-accurate-cues-with-esam.md), cue points from the CMS (or the v2-compatible
`AdOffsets` tag) are snapped to the nearest frame (`Grid.snap_to_frame`). Points on the natural segment grid
(`Grid.on_segment_grid`, every 6.006 s at 29.97 fps) need nothing. Any other point adds ESAM to the title's job, which
splits every variant at that frame (the audio is muxed into every segment, so every track splits) and bills the job on
the Professional tier. Finalize keeps a cue point as a break offset when a segment boundary that every variant shares
lies within 100 ms of it. The library stores every asset's valid break offsets; the scheduler picks from them and emits
SCTE-35 TIME_SIGNAL breaks (segmentation type 0x34, MPU UPID carrying the content id).

## Consequences
Breaks land within half a frame of the editorial cue (about ±17 ms at 29.97 fps). Each off-grid cue point adds one
short segment per variant, and the tools filter the `SEGMENT_DURATION_MISMATCH` warnings it causes. Titles with
off-grid cue points encode at the Professional rate (about $0.06 instead of $0.0375 per content minute on
`standard`); cue points on the segment grid keep the Basic rate.
