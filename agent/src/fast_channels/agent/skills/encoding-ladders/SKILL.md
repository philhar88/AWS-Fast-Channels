---
name: encoding-ladders
description: Encoding ladder rules - AVC/HEVC/AV1 renditions, manifest profiles, the propose then provision flow, MediaConvert encode cost and what a new ladder version implies for channels. Use when the operator asks about quality, codecs, ladders, ingest cost or a new encoding profile.
---
# Encoding ladders

## What a ladder is
- One CMAF encode per asset holds every codec ladder: **AVC** (universal), **HEVC** (`hvc1`, Apple and smart
  TVs), **AV1** (10-bit, best compression: about half the bitrate of AVC and about 20% less than HEVC at equal
  quality) plus AAC audio.
- Each encode also writes per-codec and per-resolution **manifest profiles** at no extra cost: `hls` (all
  codecs), `hls-avc`, `hls-hevc`, `hls-av1`, `hls-sd`, `hls-hd`, `hls-uhd`, `dash-avc`, `dash-hevc`,
  `dash-av1`. Channels expose a subset as outputs.
- The ~2 Mbit/s 720p AVC rung is always first (Apple default variant); each codec family keeps a rung at or
  below 500 kbit/s so players start fast. The tools enforce these rules.
- A ladder version is `<id>@<fingerprint>`. Changing any encode setting creates a new version.

## Changing a ladder: propose, then provision
1. `encoding___propose_ladder` with a base ladder and operations (codecs, max/min height, quality shift, rungs
   to drop). It has no side effects and returns the diff, validation errors and warnings, and the encode
   cost per content-minute. Show all of it to the operator.
2. Only if the operator accepts and there are no errors, `encoding___provision_ladder` with the
   `proposal_id`. This needs approval. It creates the MediaConvert template **and** the ladder kit: filler
   slate, break slates, house ads and the ad pod, all encoded on the new ladder. Provisioning takes minutes.
3. A new ladder only reaches air on a channel whose ladder is that version, with assets ingested on it.
   Existing assets must be re-ingested (approval, encode cost) to play on the new ladder.

## Cost
- Encoding is billed per output minute; quote the tool's `cost_per_minute_usd` or ingest estimate times the
  content duration. 4K (above 1080 lines) and AV1 tiers cost more per minute than HD AVC.
- Fewer rungs or fewer codecs lower encode and storage cost but reduce device reach or quality. Say which.

## Device notes
- AV1 needs recent devices; Roku plays AV1 only through DASH. Keep AVC in every ladder.
- hls.js prefers HEVC over AV1 on the combined playlist; per-codec profiles let a player force a codec.
