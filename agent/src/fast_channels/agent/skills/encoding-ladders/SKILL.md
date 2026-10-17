---
name: encoding-ladders
description: Encoding ladder rules - AVC/HEVC/AV1 renditions, manifest profiles, caption tracks, the propose then provision flow, ingesting content with its captions, MediaConvert encode cost and what a new ladder version implies for channels. Use when the operator asks about quality, codecs, captions or subtitles, ladders, ingest, encode cost or a new encoding profile.
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
- A ladder version is `<id>@<fingerprint>` and never changes. Changing any encode setting creates a new version.
  `encoding___list_ladders` lists them; `encoding___get_ladder` shows every rendition by name.

## Captions
- A ladder lists its caption tracks (IMSC1 text, one per language; `encoding___get_ladder` shows them). Every
  asset of the ladder has every track, empty when it has no captions, because Channel Assembly drops ad breaks
  between sources whose tracks differ. Adding or removing a track is a new ladder version (`ops.captions`).
- Each track's source is recorded per asset (`library___get_asset` `captions`; `library___find_assets`
  `captioned` lists the languages that have captions): `provided` (a file that came with the title), `embedded`
  (CEA-608/708 in the source), `generated` (transcribed by the captioner engine: say it is machine-made and worth
  a human check where captions are regulated) or `none` (an empty track).
- When ingesting, pass caption files the catalog or operator names in `captions.files` (uri relative to the
  source's folder or s3:// in the ingest bucket, and its language); otherwise the pipeline looks in the source and
  then generates the spoken language. `captions.mode`: `auto` (default), `generate` (ignore captions in the
  source), `off` (empty tracks). Never invent caption files.

## Changing a ladder: propose → review → provision
1. `encoding___propose_ladder` with a `base` version and `ops` (codecs to keep, max/min height, quality_shift
   -2..+2, rung names to drop, new_id). It changes nothing and returns a `proposal_id` (valid 24 hours), `valid`,
   the diff, errors, warnings, the encode cost per content-minute and the kit cost. Show all of it.
2. Only when the proposal is valid and the operator accepts it: `encoding___provision_ladder` with the
   `proposal_id`. It needs approval. It creates the MediaConvert job template **and** encodes the ladder kit
   (filler slate, break slates, house ads and the ad pod) with the new version, which costs `kit_cost_usd` and
   takes minutes. Poll `encoding___get_ladder` until the status is READY.
3. A new version reaches air only on a channel created with it (`channels___create_channel` requires READY),
   with assets encoded with it. Assets of other versions must be ingested again.

## Ingesting content
- `library___ingest` (approval) encodes a mezzanine from the ingest bucket or an https URL with a ladder
  version that is PROVISIONING or READY; use the ladder of the channel that will air it. Pass the catalog
  `duration_ms` to get a cost estimate, and quote it before asking for approval.
- It returns at once; poll `library___get_ingest_status` with the `execution_name` until `done`, then check that
  the asset is READY.

## Cost
- Encoding is billed per output minute; quote the tool's `cost_per_minute_usd` or `estimated_cost_usd`.
  4K (above 1080 lines) and AV1 tiers cost more per minute than HD AVC.
- Fewer rungs or fewer codecs lower encode and storage cost but reduce device reach or quality. Say which.

## Device notes
- AV1 needs recent devices; Roku plays AV1 only through DASH. Keep AVC in every ladder.
- hls.js prefers HEVC over AV1 on the combined playlist; per-codec profiles let a player force a codec.
