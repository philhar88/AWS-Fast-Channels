---
name: channel-assembly
description: MediaTailor Channel Assembly rules for FAST channels - creating, starting and stopping channels, outputs and source groups, ladder consistency, LINEAR playout with filler, ad breaks, lead time, alerts and running cost. Use before creating, starting, stopping or scheduling channels.
---
# Channel Assembly

## Channels
- FAST channels are **LINEAR**: programs play once, back to back, in schedule order. Gaps are filled by the
  channel's **filler slate**, which loops. Plan full blocks so viewers do not watch filler.
- **Ladder consistency is mandatory.** Programs, break slates and the filler slate on a channel must be encoded
  with the channel's exact ladder version (same renditions, order, codecs, resolution, frame rate). Channel
  Assembly matches variants by position; a mismatch breaks playback. Only schedule assets whose
  `ladder_version` equals the channel's.
- A channel's **outputs** are source groups (rendition profiles) such as `hls`, `hls-avc`, `dash-avc`. Every
  VOD source on the channel, including the slates, needs a package configuration for every output. Pipeline
  encodes have all of them; for registered external content, add a missing group with
  `origins___update_package_configurations` (approval).
- Channels defined by the deployment (owner `iac`) can be started and stopped but not created, deleted or
  restructured by you.

## Creating a channel
1. `encoding___list_ladders`: the channel needs a **READY** ladder version (its kit holds the filler and break
   slates). A PROVISIONING version is not usable yet; wait for READY.
2. `channels___create_channel` (approval) with a short name (the channel becomes `<prefix>-agent-<name>`), the
   ladder version, the outputs and optionally a brief. It is created STOPPED and costs nothing until started.
   The deployment limits how many agent channels exist (`QUOTA`).
3. Fill it with the scheduling loop, then `channels___start_channel` (approval).
- `channels___delete_channel` (approval, admins) removes an agent channel with its programs and guide; it
  cannot be undone.

## Running cost
- A channel bills only while **RUNNING**: BASIC tier about $0.10 per channel-hour, STANDARD about $0.35.
  24/7 BASIC is about $73 per month.
- `channels___start_channel` needs approval and schedules an automatic stop after the deployment's maximum
  runtime (`auto_stop_at`). `channels___stop_channel` (approval) stops playback for every viewer.
- Programs are published at absolute start times. Starting a stopped channel removes programs whose start has
  already passed (they would otherwise air late and be cut); the result lists them in `skipped`. Tell the
  operator which programs were skipped and offer to plan that window again; everything later airs on time.
- A running channel cannot change its outputs or tier.

## Programs and timing
- Programs are runtime data, not infrastructure. MediaTailor **deletes aired programs about 15 minutes after
  they finish**; the published guide keeps the history. Never treat a missing aired program as an error.
- Schedule at least **10 minutes ahead** of now. Later inserts usually work but are best effort; the plan tool
  warns about short lead time.
- The publish tool links a block after the program that precedes it in time and returns `PLAN_STALE` if the
  schedule moved since you planned. Plan again when that happens.

## Ad breaks
- A break plays a slate whose length is the break policy's `slate_gops` (15, 30, 45 or 60 GOPs, about 30, 60,
  90 or 120 s).
- The plan tool chooses every break offset from the asset's valid offsets (segment boundaries common to all
  renditions) near the policy's cadence. You only choose the policy: `cadence_minutes`, `min_gap_minutes`,
  `max_breaks`, `slate_gops`.
- SCTE-35 signalling (TIME_SIGNAL) and event ids are deterministic per program; never set them yourself.

## Alerts
- `origins___list_alerts` for the channel after every schedule change and before starting it. SCHEDULING_ERROR
  alerts break playback and must be fixed; the expected SEGMENT_DURATION_MISMATCH and BANDWIDTH_MISMATCH
  warnings are filtered out.
