# 0008 — Ad breaks on segment boundaries; ESAM only for off-grid cue points

**Status:** Accepted

## Context
Channel Assembly inserts ad breaks at segment boundaries common to every track. MediaConvert ESAM can force extra
boundaries, but it makes jobs Professional tier, makes DASH multi-period, shortens segments (flooding Channel Assembly with
SEGMENT_DURATION_MISMATCH alerts), and AV1 and audio tracks honour only points on the GOP grid.

## Decision
Cue points from the CMS (or the v2-compatible `AdOffsets` tag) are snapped to the 2 s GOP grid. Points already on the
6 s segment grid need nothing; ESAM is added only for points between segment boundaries. The library stores every asset's
valid break offsets; the scheduler picks from them and emits SCTE-35 TIME_SIGNAL breaks (segmentation type 0x34, MPU UPID
carrying the content id).

## Consequences
Breaks land within one GOP of the editorial cue. Alert filtering in the tools hides the expected mismatch warnings.
