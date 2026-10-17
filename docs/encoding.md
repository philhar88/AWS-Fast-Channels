# Encoding: the ladder, the HLS output and cost

Every title, slate and house ad is encoded **once per ladder version** by AWS Elemental MediaConvert into one Apple
HLS output group: MPEG-TS segments with H.264/AVC video and AAC-LC audio muxed into every variant. Channel Assembly
reads that encode's own multivariant playlist. This page describes the ladder model, the built-in ladder, the
MediaConvert settings, what an encode writes, how cue points become ad breaks, and what an encode costs.

- [Why one ladder per channel](#why-one-ladder-per-channel)
- [The ladder model](#the-ladder-model)
- [The standard ladder](#the-standard-ladder)
- [MediaConvert settings](#mediaconvert-settings)
- [What an encode writes](#what-an-encode-writes)
- [Frame rates, interlace and HDR](#frame-rates-interlace-and-hdr)
- [Cue points and the segment grid](#cue-points-and-the-segment-grid)
- [What the pipeline verifies and fixes](#what-the-pipeline-verifies-and-fixes)
- [Ladder kits](#ladder-kits)
- [Cost](#cost)
- [Customising ladders](#customising-ladders)
- [Caveats](#caveats)

## Why one ladder per channel

Channel Assembly stitches programs, filler slate and ad slates by variant position. Every VOD source on a channel
must have exactly the same renditions, in the same order, or HLS media sequences break at program boundaries.
MediaTailor's preconditioned ads must match too. So a ladder is an immutable, fingerprinted model, and a channel is
bound to one ladder version ([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)). The tools refuse to schedule an
asset from another ladder (`LADDER_MISMATCH`).

## The ladder model

A ladder is a TOML file validated by `fast_channels.core.ladder.Ladder`. The built-in ladder lives in
`core/src/fast_channels/core/ladders/` (package data, so Lambda functions load it too); `ladders/` at the repository
root is a symlink to that directory. This is `ladders/standard.toml`, the default, with each field explained:

```toml
id = "standard"
description = "AVC + AAC-LC muxed in MPEG-TS, up to 1080p, 29.97 fps"
frame_rate = "29.97"        # also "25", "30000/1001", "50", "59.94"
gop_seconds = 2             # closed GOPs: every variant has an IDR on the same 2 s grid
segment_seconds = 6         # a whole number of GOPs

[options]
allow_frame_rate_conversion = false   # true allows 50 Hz <-> 60 Hz conversion

[audio]                     # AAC-LC stereo 48 kHz CBR, muxed into every variant
kbps = 128

[[video]]                   # multivariant order: the first variant is the player's start variant
name = "avc-720p"           # also the MediaConvert NameModifier (_avc-720p) and the media playlist name
width = 1280
height = 720
qvbr = 7                    # QVBR quality level 1-10
max_kbps = 3400             # QVBR cap (MaxBitrate)

[[video]]
name = "avc-1080p"
width = 1920
height = 1080
qvbr = 8
max_kbps = 6000

[[video]]
name = "avc-360p"
width = 640
height = 360
qvbr = 6
max_kbps = 800
hrd_kbps = 800              # rungs at or below 432p: HRD buffer = cap
```

Every rendition is H.264; `profile` is `HIGH` (default) or `MAIN`, both 8-bit. There is no codec field: a ladder is
AVC with muxed AAC-LC, nothing else.

### Identity: the ladder version

`ladder.version` is `<id>@<fingerprint>`, for example `standard@843ed1d05a`. The fingerprint is the first ten hex
digits of the SHA-256 of the canonical JSON of every ladder field except `id` and `description`. Change one rung and
you get a new version, a new job template and a new kit; edit the description and nothing changes. In S3 keys and
resource names the `@` becomes `_`.

### Validation rules

`Ladder` refuses a file that breaks any of these rules:

| Rule | Why |
|---|---|
| Unique rendition names (lower-case slugs, at most 32 characters) | One media playlist per rendition |
| Width at most 1920 and height at most 1080, both even and at least 96 | The ladder tops out at 1080p, so every output is billed as SD or HD |
| `gop_seconds` and `segment_seconds` are whole seconds, the segment a multiple of the GOP, and the GOP a whole number of frames | MediaConvert `SegmentLength` is whole seconds; one IDR grid for every variant |
| Frame rate at most 60 fps | Apple HLS authoring spec 1.19 |
| Rungs at or below 432p set `hrd_kbps = max_kbps` | Keeps low-rung peaks at the cap |
| QVBR level 1–10; `profile` `HIGH` or `MAIN` | MediaConvert H.264 settings |
| `audio.kbps` from MediaConvert's AAC-LC stereo 48 kHz list (64, 80, 96, 112, 128, 160, 192, 224, 256, 288, 320) | Supported AAC combinations only |

## The standard ladder

`standard@843ed1d05a` runs at 29.97 fps with a 2.002 s GOP (60 frames) and 6.006 s segments: three AVC variants with
AAC-LC 128 kbit/s stereo muxed into each, on the MediaConvert Basic tier, about **$0.0375 per content minute**.

`BANDWIDTH` and `AVERAGE-BANDWIDTH` are the nominal values the pipeline writes into every multivariant playlist (see
[Finalize](#what-the-pipeline-verifies-and-fixes)): `BANDWIDTH = ceil(max_kbps × 1.1) + audio`,
`AVERAGE-BANDWIDTH = max_kbps + audio`.

| Variant | Profile | Resolution | QVBR | Max kbit/s | HRD kbit/s | `CODECS` | BANDWIDTH | AVERAGE-BANDWIDTH |
|---|---|---|---|---|---|---|---|---|
| `avc-720p` (start variant) | High | 1280×720 | 7 | 3400 | | `avc1.64001f,mp4a.40.2` | 3868000 | 3528000 |
| `avc-1080p` | High | 1920×1080 | 8 | 6000 | | `avc1.640028,mp4a.40.2` | 6728000 | 6128000 |
| `avc-360p` | High | 640×360 | 6 | 800 | 800 | `avc1.64001e,mp4a.40.2` | 1008000 | 928000 |

The 720p rung comes first, so players start mid-ladder and then switch up or down. The ladder is SDR (`VIDEO-RANGE`
is `SDR`). The `CODECS` levels are what MediaConvert's `CodecLevel AUTO` chose in a real encode; the ladder signature
compares codec families only.

## MediaConvert settings

`fast_channels.core.mediaconvert.compile_job_settings` compiles the ladder into the job template. Every setting stays
on MediaConvert's Basic tier.

| Setting | Value | Why |
|---|---|---|
| Output group | Apple HLS (`HLS_GROUP_SETTINGS`), `SEGMENTED_FILES`, `SegmentLength` 6, `SegmentLengthControl GOP_MULTIPLE`, `MinFinalSegmentLength` 1 | Closed GOPs on the grid make every segment exactly 3 GOPs (6.006 s); only a title's last segment is shorter |
| Container | `M3U8` (MPEG-TS) with `AudioDuration MATCH_VIDEO_DURATION`, a PCR on every PES packet, PIDs 480 (PMT), 481 (video), 482 (audio); no SCTE-35, ID3 or Nielsen data | Channel Assembly needs variants of equal length; the audio would otherwise end a few milliseconds before the video |
| Video | H.264 `QVBR` with `MaxBitrate` = `max_kbps`, `SINGLE_PASS_HQ`, closed GOP of `gop_seconds` × fps frames, `MinIInterval` 0, scene-change detection `TRANSITION_DETECTION`, CABAC, `CodecLevel AUTO`, `HrdBufferSize` = `hrd_kbps` or twice the cap | One IDR grid for every variant; QVBR keeps quality even at a lower average bitrate |
| Frame rate | `SPECIFIED` at the ladder rate with `DUPLICATE_DROP` | GOPs, segments and slates share one frame-exact grid |
| Deinterlacer | `INTERPOLATE`, mode `DEINTERLACE`, control `NORMAL` | Interlaced sources are deinterlaced, frames flagged progressive pass untouched, and the job stays on the Basic tier |
| Audio | AAC-LC CBR at `audio.kbps`, 2.0, 48 kHz, MPEG-4, language `en`, in every output | One muxed track per variant: no audio rendition groups |
| Manifests | `CODECS` in RFC 6381 form, `RESOLUTION` included, floating-point `EXTINF`, `TargetDurationCompatibilityMode SPEC_COMPLIANT`, no `EXT-X-PROGRAM-DATE-TIME`, uncompressed | MediaTailor needs `RESOLUTION`, `BANDWIDTH` and `CODECS`; `TARGETDURATION` 6 for 6.006 s segments; CloudFront compresses playlists |
| Captions | `CaptionLanguageSetting NONE` | Writes `CLOSED-CAPTIONS=NONE` on every variant (Apple 433088) |
| I-frame playlists | `IFrameOnlyManifest EXCLUDE` | Not used |

Not used: ESAM, ad markers, accelerated transcoding, audio normalisation, captions and subtitles. Each job sends
only sparse overrides (its input and destination), which CreateJob deep-merges into the template.

## What an encode writes

Under `assets/<asset>/<ladder key>/` an encode writes the multivariant playlist `main.m3u8`, one media playlist per
variant (`main_<rendition>.m3u8`) and its segments (`main_<rendition>_00001.ts`, …, Content-Type `video/MP2T`). After
[Finalize](#what-the-pipeline-verifies-and-fixes), the multivariant of a `standard` encode is:

```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-STREAM-INF:BANDWIDTH=3868000,AVERAGE-BANDWIDTH=3528000,VIDEO-RANGE=SDR,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720,FRAME-RATE=29.970,CLOSED-CAPTIONS=NONE
main_avc-720p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6728000,AVERAGE-BANDWIDTH=6128000,VIDEO-RANGE=SDR,CODECS="avc1.640028,mp4a.40.2",RESOLUTION=1920x1080,FRAME-RATE=29.970,CLOSED-CAPTIONS=NONE
main_avc-1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1008000,AVERAGE-BANDWIDTH=928000,VIDEO-RANGE=SDR,CODECS="avc1.64001e,mp4a.40.2",RESOLUTION=640x360,FRAME-RATE=29.970,CLOSED-CAPTIONS=NONE
main_avc-360p.m3u8
```

Media playlists are `EXT-X-VERSION:3`, `TARGETDURATION:6`, `PLAYLIST-TYPE:VOD`, with `#EXTINF:6.006,` for every
segment but the last, identical in every variant. There is no `EXT-X-MAP`. mediastreamvalidator passes a `standard`
encode; it reports only the ShouldAuth advisory 242528 (no subtitle group).

### The `hls` rendition profile

A *rendition profile* is a named manifest of an encode (`fast_channels.core.profiles`). There is one, `hls`, whose
manifest is `main.m3u8` with every variant of the ladder. It is static and needs no per-request manifest manipulation
([ADR-0003](adr/0003-native-rendition-profiles-no-object-lambda.md)). The catalogue stays a list, so a deployment
names the profiles it registers and a channel the outputs it serves:

1. `channel_assembly.package_profiles` (default `["hls"]`) is registered on **every** VOD source and slate as one
   `HttpPackageConfiguration` each: type `HLS`, source group `hls`, path `/assets/<asset>/<ladder key>/main.m3u8`.
2. Each channel's `outputs` is a subset of `package_profiles`; an output's `ManifestName` and `SourceGroup` are the
   profile name. HLS outputs use `DATERANGE` ad markers with a 60 s window.
3. Playback URLs end in the output name: `…/v1/master/<hash>/<channel>/hls.m3u8`.

## Frame rates, interlace and HDR

- Every output is normalised to the ladder's frame rate (`FramerateControl: SPECIFIED`, `DUPLICATE_DROP`), so GOPs,
  segments and slates share one frame-exact grid. At 29.97 fps a GOP is exactly 2002 ms, a segment 6006 ms and a
  15-GOP slate exactly 30030 ms (five segments); durations never drift over a long schedule.
- Frame rates belong to a display family: **50 Hz** (12.5, 25, 50 fps) or **60 Hz** (24, 30, 60 fps and their
  ×1000/1001 rates such as 23.976 and 29.97). Conversion inside a family is allowed and recorded as a warning on the
  asset (for example 24 → 29.97 for the sample films). **Conversion across families is refused** (it judders) unless
  the ladder sets `allow_frame_rate_conversion = true`. For 25 fps libraries, add a ladder with `frame_rate = "25"`
  ([in code](#in-code)) and bind 50 Hz channels to it.
- Interlaced sources go through MediaConvert's deinterlacer (`INTERPOLATE`, `DEINTERLACE` mode); frames flagged
  progressive pass untouched. Telecined sources are deinterlaced too, not inverse-telecined.
- HDR sources (PQ, HLG) are refused: the ladder is SDR.
- Sources without a video or an audio track are refused (every variant carries audio).
- Sources smaller than the top rung are upscaled; the asset records a warning.

## Cue points and the segment grid

Channel Assembly can only insert an ad break where **every track** has a segment boundary, within 100 ms
([ADR-0008](adr/0008-ad-break-alignment.md)). Every variant is cut on the same GOP grid and carries its audio in the
same segments, so every segment boundary is such a boundary.

1. Editorial cue points (`cue_points_ms` from the CMS, a sidecar, or the v2 `AdOffsets` S3 object tag) are snapped
   to the nearest segment boundary (`Grid.snap_to_segment`, the 6.006 s grid at 29.97 fps). A break therefore lands
   within ±3 s (half a segment) of the editorial cue: `600000` (10:00) becomes `600600`. Points that snap to 0 are
   dropped, and points that snap to the same boundary become one.
2. MediaConvert's Basic tier has no ESAM, so no segment is split and every segment but the last is 6.006 s.
3. With cue points, the asset's break offsets are the snapped points. Without, they are every interior segment
   boundary shared by all variants: about 600 candidate offsets per hour.

The library stores the valid offsets per asset (`break_offsets_ms`, plus the snapped `cue_points_ms`); the planner
never invents one. It picks the offsets closest to the channel's break cadence, and trusts editorial cue points where
the editor put them. Plain segment boundaries must also keep the break policy's margins: none in a program's first
`start_margin_seconds` (60) or last `end_margin_seconds` (30), and none at all in programs shorter than
`min_program_minutes` (3), so a short clip without cue points airs without breaks
([channel-assembly.md](channel-assembly.md#programs-and-ad-breaks)).

## What the pipeline verifies and fixes

The `Finalize` step runs after every encode:

- **The multivariant exists and has exactly the ladder's signature**: the ordered video codec family, resolution,
  frame rate (to three decimals) and audio codec (`mp4a.40.2`) of every variant (`manifests.expected_signature`).
  Anything else fails the ingest (`InvalidOutputsError`), because Channel Assembly matches variants by position.
- **Nominal BANDWIDTH.** Multivariant `BANDWIDTH` and `AVERAGE-BANDWIDTH` are rewritten to the ladder's nominal
  values (formula above). Measured values of a static slate or house ad would otherwise advertise a fraction of the
  content bitrate, and Channel Assembly copies them into the channel.
- **Duration and break offsets** are computed from the media playlists: the segment boundaries every variant shares
  exactly (`manifests.segment_boundaries_ms`), or, for an asset with cue points, those of its snapped points.
- **Ads** get an SGAI-ready copy under `ads/<ladder>/<creative_id>/`: media playlists with absolute segment URIs on
  the content host, the multivariant's child URIs kept relative (MediaTailor re-hosts only the manifests of
  preconditioned ads; see [ad-insertion.md](ad-insertion.md#preconditioned-ads)).

Content and slates are then registered as Channel Assembly VOD sources named `<asset_id>-<ladder key>` with one
package configuration per `package_profiles` entry; ads are not registered.

An asset is encoded once per ladder version. Its outputs live at a fixed path that its VOD source, and every program
scheduled from it, keep reading, so a request to encode a file again as an asset that is already `ENCODED` or `READY`
with that version is refused (`IngestRefusedError … upload the file under a new key, or pass a new asset_id`); only
a `FAILED` encode is replaced. Kit stills are the exception: they are deterministic, and their provisioner re-encodes
them.

## Ladder kits

A channel needs more than programs: a filler slate for schedule gaps, an ad-break slate per break length (a Channel
Assembly break lasts exactly as long as its slate) and ad creatives encoded on the same ladder. The
`LadderProvisioner` state machine encodes this **kit** for each ladder version, from the 1920×1080 stills in
`assets/brand/` (MediaConvert's video generator renders a still for an exact number of GOPs, a whole number of
segments):

| Kit item | Length | Use |
|---|---|---|
| Filler slate | 15 GOPs (30.03 s) | `FillerSlate` of every channel on the ladder; loops through gaps |
| Break slates | 15, 30, 45, 60 GOPs (30.03, 60.06, 90.09, 120.12 s) | `AdBreaks[].Slate`; fixes the break length, shows when an avail is not filled |
| House ads | 2 x 15 GOPs (30.03 s) | Preconditioned creatives of the static VAST pod, which repeats them to fill the longest break |
| VAST pod | | `ads/<ladder>/vast.xml` (`max-age=300`) and the beacon `ads/b.gif` |

At deploy time the `LadderKit` custom resource runs the provisioner for the default ladder and every ladder an IaC
channel uses (other built-in ladders get their job template only). It is idempotent by a fingerprint of the ladder,
the brand stills, the content host, the package profiles and the source location, so redeploys take seconds. At
runtime `encoding___provision_ladder` runs the same state machine for agent-proposed ladders. A kit costs about
**$0.25** on `standard`.

## Cost

Estimates come from `fast_channels.core.cost`: us-east-1 on-demand list prices of the Basic tier, first pricing
band, billed per output, per started second, with a 10 s minimum. They are what the tools report
(`library___ingest`, `encoding___propose_ladder`); your bill is authoritative.

| Ladder | Tier | Per content minute | Per content hour | Kit (once per ladder version) |
|---|---|---|---|---|
| `standard` | Basic | $0.0375 | $2.25 | about $0.25 |

Where the money goes, per content minute: `avc-720p` $0.015 and `avc-1080p` $0.015 (HD), `avc-360p` $0.0075 (SD).
The audio is muxed into each variant's output, so it adds no output of its own. The multi-codec CMAF ladder this
replaced cost about $0.56 per content minute on the Professional tier
([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)).

AVC on the Basic tier, per output minute:

| Frame rate | SD (< 720 lines) | HD (720–1080 lines) |
|---|---|---|
| Up to 30 fps | $0.0075 | $0.015 |
| Above 30, up to 60 fps (×1.25) | $0.0094 | $0.0188 |

Landscape outputs are measured vertically, portrait outputs horizontally. To cut cost further, drop rungs your
audience does not need: `standard` without `avc-1080p` costs $0.0225 per minute (the agent's `propose_ladder` reports
the exact figure).

## Customising ladders

### With the agent

The agent can derive a ladder from an existing version, provision it and build a channel on it. Every step that
creates resources or costs money needs approval.

1. **Propose.** `encoding___propose_ladder` takes a base version and operations: cap `max_height`, floor
   `min_height`, `quality_shift` QVBR by −2 to +2 (clamped to 1–10), `drop_rungs` by name, and optionally a `new_id`.
   It changes nothing; it returns the diff, validation errors and warnings, the cost per minute next to the base
   ladder's, the kit cost, and a `proposal_id` valid for 24 hours. Operations only remove rungs or shift their
   quality; new rungs need a TOML file.
2. **Provision** (approval). `encoding___provision_ladder` creates the MediaConvert job template (category
   `<prefix>`, tagged `fast:owner=agent`) and encodes the kit. The version becomes `READY` in a few minutes.
3. **Re-encode** (approval). `library___ingest` with the new `ladder_version` encodes each title the channel needs,
   from the asset's `source_uri` (`library___get_asset`). For the sample titles that is the staged copy
   `s3://<IngestBucketName>/samples/<content id>.<ext>`: their publishers' servers refuse MediaConvert's fetcher.
4. **Create a channel** (approval). `channels___create_channel` builds an agent-owned `<prefix>-agent-<name>`
   channel on the ready ladder, with its filler slate, playback configuration, logs and SigV4 channel policy. Then plan,
   publish and start it as usual.

For example: *"Propose a ladder up to 720p from standard for our legacy set-top boxes, then provision it, re-encode
the kids titles with it and create a channel called stb."*

IaC channels (`channels.iac`) are bound to built-in ladders only. Runtime job templates and kits are deleted by the
reaper when the stack is deleted.

### In code

1. Add `core/src/fast_channels/core/ladders/<name>.toml` (visible as `ladders/<name>.toml`), for example a copy of
   `standard.toml` with `id = "standard-25"` and `frame_rate = "25"` for a 50 Hz library (2 s GOPs of 50 frames,
   6.000 s segments).
2. Check it: `uv run python -c "from fast_channels.core.ladder import Ladder; l = Ladder.builtin('<name>'); print(l.version, l.problems())"`.
3. Add it to `ladders.builtin` in `cdk.json` and point channels at it with `"ladder": "<name>"`.
4. `make deploy` creates the job template and encodes the kit; then ingest titles with the new version.

Changing a built-in ladder's file changes its version: existing assets keep the old version and must be re-encoded
before a channel can switch.

## Caveats

- **Player support.** H.264 High with AAC-LC in MPEG-TS plays on practically every HLS player. There is no DASH, HEVC,
  AV1 or 4K output.
- **No captions or subtitles.** Every variant declares `CLOSED-CAPTIONS=NONE` and there are no subtitle renditions
  ([ADR-0016](adr/0016-plain-hls-ts-avc-aac-basic-tier.md)).
- **SGAI in hls.js.** hls.js 1.7.3 stalls entering an append-in-place interstitial when AAC audio is muxed into the
  content segments; SSAI and other players are not affected
  ([ad-insertion.md](ad-insertion.md#monitoring-and-troubleshooting)).
- **Untagged sources** raise MediaConvert colour-space warnings 230001/230005. They are harmless.
