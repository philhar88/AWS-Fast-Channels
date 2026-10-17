---
name: encoding-ladders
description: Encoding ladder rules - AVC renditions with muxed AAC in plain HLS, the propose then provision flow, ingesting content, MediaConvert encode cost and what a new ladder version implies for channels. Use when the operator asks about quality, resolutions, ladders, ingest, encode cost or a new encoding profile.
---
# Encoding ladders

## What a ladder is
- One plain HLS encode per asset: MPEG-TS segments, every variant **H.264 (AVC)** video with **AAC-LC** stereo
  audio muxed into the same segments. There are no other codecs, no DASH, no 4K and no captions or subtitles; do
  not offer them.
- The default ladder, `standard`, has three rungs in this order: `avc-720p` (about 3.4 Mbit/s, first, so players
  start mid-ladder), `avc-1080p` (about 6 Mbit/s) and `avc-360p` (about 0.8 Mbit/s); 29.97 fps, 2 s GOPs, 6 s
  segments, at most 1080p.
- Each encode writes one manifest profile, `hls`, which channels publish as their output.
- A ladder version is `<id>@<fingerprint>` and never changes. Changing any encode setting creates a new version.
  `encoding___list_ladders` lists them; `encoding___get_ladder` shows every rendition by name.

## Changing a ladder: propose → review → provision
1. `encoding___propose_ladder` with a `base` version and `ops`: `max_height` (cap), `min_height` (floor),
   `quality_shift` (-2..+2 QVBR levels), `drop_rungs` (rendition names) and `new_id`. It only removes rungs or
   shifts their quality; new rungs need a ladder file in the repository. It changes nothing and returns a
   `proposal_id` (valid 24 hours), `valid`, the diff, errors, warnings, the encode cost per content-minute and the
   kit cost. Show all of it.
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
- Editorial cue points are frame-accurate: a break airs on the frame of its cue. A cue point between 6 s segment
  boundaries adds ESAM to that title's encode, which splits every rendition at that frame and bills the whole
  encode on MediaConvert's Professional tier; cue points on segment boundaries need nothing. The tool returns
  `pricing_tier` (`BASIC` or `PROFESSIONAL`) with its estimate: say which before asking for approval.
- It returns at once; poll `library___get_ingest_status` with the `execution_name` until `done`, then check that
  the asset is READY.

## Cost
- Encoding is billed per output minute on the MediaConvert Basic tier: about $0.015 per minute for each HD rung
  (720p and above) and $0.0075 for each SD rung, so `standard` costs about $0.0375 per content minute. A title
  whose cue points need ESAM is billed on the Professional tier, about $0.06 per content minute on `standard`
  (about 1.6 times). Ladder costs from `encoding___list_ladders` and `encoding___propose_ladder` are Basic-tier.
  Quote the tool's `cost_per_minute_usd` or `estimated_cost_usd`, never your own arithmetic.
- Dropping a rung lowers encode and storage cost: without `avc-1080p` there is no full-HD picture, without
  `avc-360p` viewers on slow connections buffer more. Say which.
