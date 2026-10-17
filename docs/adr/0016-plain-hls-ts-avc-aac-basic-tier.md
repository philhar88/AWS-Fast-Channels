# 0016 — Plain HLS MPEG-TS, AVC + AAC muxed, Basic tier

**Status:** Accepted (supersedes [0006](0006-one-cmaf-encode-per-asset.md), [0013](0013-captions-roadmap.md),
[0014](0014-captions-as-ladder-tracks.md) and [0015](0015-pluggable-captioner-elemental-inference.md); amends
[0003](0003-native-rendition-profiles-no-object-lambda.md), [0005](0005-preconditioned-ads.md) and
[0008](0008-ad-break-alignment.md))

## Context
The first v3.0.0 encode was one CMAF output group with AV1, HEVC and AVC (16 video renditions), three AAC audio
renditions, IMSC1 captions, HLS and DASH manifests per rendition profile, loudness normalisation, accelerated
transcoding and ESAM for off-grid cue points. It ran on MediaConvert's Professional tier at about $0.56 per content
minute ($33.60 per hour), and each feature brought an AWS behaviour that the pipeline had to work around or document:
AV1 `av1C` levels, HE-AAC v2 declared as HE-AAC in `CODECS` (535060), IMSC1 tracks cut differently from the video after
an ESAM split, blank WebVTT in subtitle renditions during SSAI ads, subtitle sequence numbers that drifted from the
video's, DASH ad `BaseURL` handling, and hls.js caption bugs.

FAST channels earn their money from ads, and SSAI is the path every player supports. What SSAI needs is a ladder that
Channel Assembly, MediaTailor and every player handle without surprises:

- Channel Assembly supports MPEG-TS VOD sources (AWS's own examples are TS). An ad-break offset must be within 100 ms
  of a segment boundary on every track, which is automatic when the audio is muxed into the video's segments.
- MediaTailor stitches preconditioned ads (`StreamingMediaFileConditioning NONE`) when they match the content's
  variants: bitrate, resolution and codec.
- H.264 High with AAC-LC in TS plays on practically every HLS player.

## Decision
- **One Apple HLS output group of MPEG-TS segments.** Each variant is one MediaConvert output: H.264 High 8-bit with
  AAC-LC 128 kbit/s stereo 48 kHz muxed into the same transport stream. No audio rendition groups, no `EXT-X-MAP`.
- **The `standard` ladder**: `avc-720p` (3.4 Mbit/s, first, the start variant), `avc-1080p` (6 Mbit/s) and `avc-360p`
  (0.8 Mbit/s); 29.97 fps, 2 s closed GOPs, 6.006 s segments, at most 1080p. The `uhd` ladder is removed.
- **Basic tier only**: AVC single-pass HQ with QVBR, and none of ESAM, accelerated transcoding, loudness
  normalisation, captions or I-frame playlists. About $0.0375 per content minute ($2.25 per hour).
- **Cue points snap to the segment grid** (`Grid.snap_to_segment`), because there is no ESAM on the Basic tier.
- **No captions or subtitles.** Every variant declares `CLOSED-CAPTIONS=NONE`.
- **One rendition profile, `hls`**: the encode's own `main.m3u8`, one package configuration per VOD source and one
  channel output by default.
- **Unchanged**: the ladder version (`<id>@<fingerprint>`) is the unit of compatibility, channels bind to one version
  and tools refuse other versions (`LADDER_MISMATCH`); S3 → MediaConvert → S3 → Channel Assembly → MediaTailor
  (`PLAYER_SELECT`, SSAI and SGAI) → CloudFront; preconditioned house ads from the per-ladder kit.

## Consequences
- Encoding costs about 15 times less, and a kit about $0.25 instead of $3.56. A real `standard` encode passes
  mediastreamvalidator with only a ShouldAuth advisory (242528, no subtitle group).
- Breaks land within ±3 s (half a segment) of the editorial cue instead of within one GOP.
- No captions or subtitles until a future decision adds them, for example CEA-608 carried in the AVC video, which
  MPEG-TS supports. Outlets that require captions cannot use this release as it is.
- DASH is dropped: DASH-only devices need an HLS player. Without HEVC and AV1, delivery needs more bits for the same
  picture quality, and there is no 4K.
- SSAI is unaffected by the muxed audio. hls.js 1.7.3 stalls entering an append-in-place SGAI interstitial when AAC
  audio is muxed; players other than hls.js are not affected. Verify SGAI on your player.
- Every ladder version changes: kits re-provision and assets encoded with the CMAF ladders must be re-encoded.
- The ladder model loses its codec, audio-group and caption fields; Finalize keeps only the signature check, nominal
  `BANDWIDTH`, durations and break offsets, and the SGAI ad copies. Apple's Should-level player guidance (a cellular
  floor variant, two variants at the top resolution) is not enforced.
