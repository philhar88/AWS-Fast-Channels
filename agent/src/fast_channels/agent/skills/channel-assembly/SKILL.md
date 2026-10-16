---
name: channel-assembly
description: MediaTailor Channel Assembly rules for FAST channels - source groups, ladder consistency, LINEAR playout with filler, ad breaks, lead time, aired-program cleanup and channel running cost. Use before creating, starting, stopping or scheduling channels.
---
# Channel Assembly

## Channels
- FAST channels are **LINEAR**: programs play once, back to back, in schedule order. Gaps are filled by the
  channel's **filler slate**, which loops. Plan full blocks so viewers do not watch filler.
- A channel's **outputs** are source groups (manifest profiles) such as `hls`, `hls-avc`, `dash-avc`. Every VOD
  source on the channel, including filler and break slates, needs a package configuration for every output.
  The tools register them; if a tool reports a missing package configuration, re-register the asset rather
  than editing the channel.
- **Ladder consistency is mandatory.** Programs, break slates and the filler slate on a channel must be encoded
  with the channel's exact ladder version (same renditions, order, codecs, resolution, frame rate). Channel
  Assembly matches variants by position; a mismatch breaks playback. Only schedule assets whose
  `ladder_version` equals the channel's.
- Channels created by the agent are named `<prefix>-agent-*`; channels defined in infrastructure cannot be
  deleted or restructured by you.

## Running cost
- A channel bills only while **RUNNING**: BASIC tier about $0.10 per channel-hour, STANDARD about $0.35.
  24/7 BASIC is about $73 per month. Starting a channel needs approval; agent-started channels stop
  automatically after the deployment's maximum runtime.
- A running channel cannot change its outputs or tier. Restarting a stopped channel replays programs that
  have not yet been cleaned up.

## Programs and timing
- Programs are runtime data, not infrastructure. MediaTailor **deletes aired programs about 15 minutes after
  they finish**; the published EPG keeps the history. Never treat a missing aired program as an error.
- Schedule at least **10 minutes ahead** of now. Late inserts usually work but are best effort; the plan tool
  warns about short lead time.
- A new program must follow the program that precedes it in time; the publish tool re-anchors the plan and
  returns `PLAN_STALE` if the schedule moved since you planned. Plan again when that happens.
- Clipped programs must keep at least 30 seconds of content.

## Ad breaks
- A break plays a slate whose duration is the break length (about 30, 60, 90 or 120 s on the ladder grid).
- Break offsets must sit on segment boundaries common to all renditions; the plan tool snaps and validates
  them. Offset 0 is a pre-roll.
- SCTE-35 signalling (TIME_SIGNAL) and event ids are deterministic per program; never set them yourself.
