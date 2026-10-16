# Encoding: ladders, rendition profiles and cost

Every title, slate and house ad is encoded **once per ladder version** by AWS Elemental MediaConvert into one CMAF
output group that carries AV1, HEVC and AVC video with AAC audio. HLS and DASH manifests for every rendition profile
come from that single encode. This page describes the ladder model, the built-in ladders, how manifests become
Channel Assembly source groups, and what an encode costs.

- [Why one ladder per channel](#why-one-ladder-per-channel)
- [The ladder model](#the-ladder-model)
- [Built-in ladders](#built-in-ladders)
- [Rendition profiles](#rendition-profiles)
- [Frame rates, interlace and HDR](#frame-rates-interlace-and-hdr)
- [Cue points and the ESAM rule](#cue-points-and-the-esam-rule)
- [What the pipeline verifies and fixes](#what-the-pipeline-verifies-and-fixes)
- [Ladder kits](#ladder-kits)
- [Cost](#cost)
- [Customising ladders](#customising-ladders)
- [Codec caveats](#codec-caveats)

## Why one ladder per channel

Channel Assembly stitches programs, filler slate and ad slates by variant position. Every VOD source on a channel
must have exactly the same renditions, in the same order, or HLS media sequences break at program boundaries.
MediaTailor's preconditioned ads must match too. So a ladder is an immutable, fingerprinted model, and a channel is
bound to one ladder version ([ADR-0006](adr/0006-one-cmaf-encode-per-asset.md)). The tools refuse to schedule an
asset from another ladder (`LADDER_MISMATCH`).

## The ladder model

A ladder is a TOML file validated by `fast_channels.core.ladder.Ladder`. The built-in ladders live in
`core/src/fast_channels/core/ladders/` (package data, so Lambda functions load them too); `ladders/` at the repository
root is a symlink to that directory.

```toml
id = "standard"
description = "AVC + HEVC + AV1 up to 1080p, 29.97 fps"
frame_rate = "29.97"        # also "25", "30000/1001", "50", "59.94"
gop_seconds = 2             # every codec gets an IDR on the same 2 s grid
segment_seconds = 6

[options]
iframe_playlists = false    # off until verified through Channel Assembly and ad insertion
loudness_lkfs = -24.0       # ITU BS.1770-3; null disables normalisation
acceleration = "PREFERRED"  # MediaConvert accelerated transcoding: PREFERRED | ENABLED | DISABLED
film_grain = false          # AV1 film-grain synthesis
allow_frame_rate_conversion = false

[[video]]                   # multivariant order: the lowest AVC 720p rung first
name = "avc-720p-a"
codec = "avc"               # avc | hevc | av1
width = 1280
height = 720
qvbr = 7                    # QVBR quality level 1-10
max_kbps = 3400             # QVBR cap
audio_group = "aac-lc-128"

[[video]]
name = "avc-432p"
codec = "avc"
width = 768
height = 432
qvbr = 7
max_kbps = 1100
hrd_kbps = 1100             # HRD buffer = cap on rungs <= 432p
audio_group = "aac-he1-64"

[[video]]
name = "av1-1080p"
codec = "av1"
width = 1920
height = 1080
qvbr = 7
max_kbps = 3500
audio_group = "aac-lc-128"
bit_depth = 10

[[audio]]
group_id = "aac-lc-128"
profile = "lc"              # lc | he1 | he2
kbps = 128
```

### Identity: the ladder version

`ladder.version` is `<id>@<fingerprint>`, for example `standard@86bf8088e0`. The fingerprint is the first ten hex
digits of the SHA-256 of the canonical JSON of everything that affects the encode (all fields except `id` and
`description`). Change one rung and you get a new version, a new job template and a new kit; edit the description and
nothing changes. In S3 keys and resource names the `@` becomes `_`.

### Validation rules

`Ladder` refuses a file that breaks any of these rules (each one comes from a live finding or a player requirement):

| Rule | Why |
|---|---|
| Unique rendition names; every `audio_group` exists and every audio group is used | One manifest per rendition, no orphan audio |
| `gop_seconds` and `segment_seconds` are whole seconds, the segment a multiple of the GOP, and the GOP a whole number of frames | MediaConvert fragment and segment lengths; one IDR grid for all codecs |
| Frame rate at most 60 fps | Apple HLS authoring spec 1.19 |
| The first rendition is the lowest-bitrate AVC 720p rung | Apple HLS 1.32: the first variant is the default start variant |
| Each codec's lowest variant is at most 500 kbit/s (nominal `BANDWIDTH`) | hls.js skips a codec at start when its lowest variant is above that |
| AVC/HEVC rungs at or below 432p set `hrd_kbps = max_kbps`; AV1 has no HRD setting | Keeps low-rung peaks at the cap |
| HEVC within level 5.1; AVC and AV1 within 4096×2160 | Apple HLS 1.6b; MediaConvert output limits |
| AVC is 8-bit; HEVC Main (8-bit) or Main10; AV1 Main 8- or 10-bit | Codec profiles MediaConvert supports |
| AAC bitrates from MediaConvert's stereo 48 kHz list; HE-AAC at most 64 kbit/s | Apple HLS 2.4 |
| `film_grain` only with AV1 renditions | Option applies to AV1 only |

## Built-in ladders

Both ladders run at 29.97 fps with a 2.002 s GOP (60 frames) and 6.006 s segments. `BANDWIDTH` and
`AVERAGE-BANDWIDTH` are the nominal values the pipeline writes into every multivariant playlist (see
[Finalize](#what-the-pipeline-verifies-and-fixes)): `BANDWIDTH = ceil(max_kbps × 1.1) + audio`,
`AVERAGE-BANDWIDTH = max_kbps + audio`.

### `standard` (default): AVC + HEVC + AV1 up to 1080p

`standard@86bf8088e0`: 16 video renditions and 3 audio renditions, MediaConvert Professional tier, about **$0.56 per
content minute**.

| Rendition | Codec | Resolution | QVBR | Max kbit/s | HRD kbit/s | Bit depth | Audio | BANDWIDTH | AVERAGE-BANDWIDTH |
|---|---|---|---|---|---|---|---|---|---|
| `avc-720p-a` | AVC High | 1280×720 | 7 | 3400 | | 8 | `aac-lc-128` | 3868000 | 3528000 |
| `avc-1080p-b` | AVC High | 1920×1080 | 9 | 9000 | | 8 | `aac-lc-128` | 10028000 | 9128000 |
| `avc-1080p-a` | AVC High | 1920×1080 | 8 | 7000 | | 8 | `aac-lc-128` | 7828000 | 7128000 |
| `avc-720p-b` | AVC High | 1280×720 | 8 | 5000 | | 8 | `aac-lc-128` | 5628000 | 5128000 |
| `avc-540p` | AVC High | 960×540 | 7 | 2200 | | 8 | `aac-lc-128` | 2548000 | 2328000 |
| `avc-432p` | AVC High | 768×432 | 7 | 1100 | 1100 | 8 | `aac-he1-64` | 1274000 | 1164000 |
| `avc-360p` | AVC High | 640×360 | 6 | 600 | 600 | 8 | `aac-he1-64` | 724000 | 664000 |
| `avc-234p` | AVC High | 416×234 | 6 | 120 | 120 | 8 | `aac-he2-32` | 164000 | 152000 |
| `hevc-1080p` | HEVC Main | 1920×1080 | 8 | 5500 | | 8 | `aac-lc-128` | 6178000 | 5628000 |
| `hevc-720p` | HEVC Main | 1280×720 | 7 | 2800 | | 8 | `aac-lc-128` | 3208000 | 2928000 |
| `hevc-540p` | HEVC Main | 960×540 | 7 | 1400 | | 8 | `aac-lc-128` | 1668000 | 1528000 |
| `hevc-360p` | HEVC Main | 640×360 | 6 | 330 | 330 | 8 | `aac-he1-64` | 427000 | 394000 |
| `av1-1080p` | AV1 Main | 1920×1080 | 7 | 3500 | | 10 | `aac-lc-128` | 3978000 | 3628000 |
| `av1-720p` | AV1 Main | 1280×720 | 7 | 2000 | | 10 | `aac-lc-128` | 2328000 | 2128000 |
| `av1-540p` | AV1 Main | 960×540 | 7 | 1000 | | 10 | `aac-lc-128` | 1228000 | 1128000 |
| `av1-360p` | AV1 Main | 640×360 | 6 | 300 | | 10 | `aac-he1-64` | 394000 | 364000 |

Audio: `aac-lc-128` (AAC-LC 128 kbit/s), `aac-he1-64` (HE-AAC 64 kbit/s), `aac-he2-32` (HE-AACv2 32 kbit/s), all
stereo 48 kHz, loudness-normalised to −24 LKFS.

### `uhd`: AVC to 1080p, HEVC Main10 and AV1 10-bit to 2160p

`uhd@5306b91dd0`: 22 video renditions and 4 audio renditions, Professional tier, about **$1.36 per content minute**.
Enable it with `ladders.builtin: ["standard", "uhd"]` and bind a channel to it with `ladder: "uhd"`.

| Rendition | Codec | Resolution | QVBR | Max kbit/s | HRD kbit/s | Bit depth | Audio | BANDWIDTH | AVERAGE-BANDWIDTH |
|---|---|---|---|---|---|---|---|---|---|
| `avc-720p-a` | AVC High | 1280×720 | 7 | 3400 | | 8 | `aac-lc-128` | 3868000 | 3528000 |
| `avc-1080p-b` | AVC High | 1920×1080 | 9 | 9000 | | 8 | `aac-lc-160` | 10060000 | 9160000 |
| `avc-1080p-a` | AVC High | 1920×1080 | 8 | 7000 | | 8 | `aac-lc-160` | 7860000 | 7160000 |
| `avc-720p-b` | AVC High | 1280×720 | 8 | 5000 | | 8 | `aac-lc-128` | 5628000 | 5128000 |
| `avc-540p` | AVC High | 960×540 | 7 | 2200 | | 8 | `aac-lc-128` | 2548000 | 2328000 |
| `avc-432p` | AVC High | 768×432 | 7 | 1100 | 1100 | 8 | `aac-he1-64` | 1274000 | 1164000 |
| `avc-360p` | AVC High | 640×360 | 6 | 600 | 600 | 8 | `aac-he1-64` | 724000 | 664000 |
| `avc-234p` | AVC High | 416×234 | 6 | 120 | 120 | 8 | `aac-he2-32` | 164000 | 152000 |
| `hevc-2160p-b` | HEVC Main10 | 3840×2160 | 9 | 20000 | | 10 | `aac-lc-160` | 22160000 | 20160000 |
| `hevc-2160p-a` | HEVC Main10 | 3840×2160 | 8 | 15000 | | 10 | `aac-lc-160` | 16660000 | 15160000 |
| `hevc-1440p` | HEVC Main10 | 2560×1440 | 8 | 9000 | | 10 | `aac-lc-160` | 10060000 | 9160000 |
| `hevc-1080p` | HEVC Main10 | 1920×1080 | 8 | 5500 | | 10 | `aac-lc-160` | 6210000 | 5660000 |
| `hevc-720p` | HEVC Main10 | 1280×720 | 7 | 2800 | | 10 | `aac-lc-128` | 3208000 | 2928000 |
| `hevc-540p` | HEVC Main10 | 960×540 | 7 | 1400 | | 10 | `aac-lc-128` | 1668000 | 1528000 |
| `hevc-360p` | HEVC Main10 | 640×360 | 6 | 330 | 330 | 10 | `aac-he1-64` | 427000 | 394000 |
| `av1-2160p-b` | AV1 Main | 3840×2160 | 8 | 16000 | | 10 | `aac-lc-160` | 17760000 | 16160000 |
| `av1-2160p-a` | AV1 Main | 3840×2160 | 7 | 10000 | | 10 | `aac-lc-160` | 11160000 | 10160000 |
| `av1-1440p` | AV1 Main | 2560×1440 | 7 | 5500 | | 10 | `aac-lc-160` | 6210000 | 5660000 |
| `av1-1080p` | AV1 Main | 1920×1080 | 7 | 3500 | | 10 | `aac-lc-160` | 4010000 | 3660000 |
| `av1-720p` | AV1 Main | 1280×720 | 7 | 2000 | | 10 | `aac-lc-128` | 2328000 | 2128000 |
| `av1-540p` | AV1 Main | 960×540 | 7 | 1000 | | 10 | `aac-lc-128` | 1228000 | 1128000 |
| `av1-360p` | AV1 Main | 640×360 | 6 | 300 | | 10 | `aac-he1-64` | 394000 | 364000 |

Audio: `aac-lc-160`, `aac-lc-128`, `aac-he1-64`, `aac-he2-32`. Both ladders are SDR (`VIDEO-RANGE` stays SDR,
including HEVC Main10 and AV1 10-bit).

## Rendition profiles

A *rendition profile* is a named filter over a ladder: a manifest format, an optional codec set and a height range
(`fast_channels.core.profiles`). Every encode writes one HLS multivariant playlist and one DASH MPD per applicable
profile through MediaConvert `AdditionalManifests`, next to the shared segments. That costs nothing extra and needs no
per-request manifest manipulation ([ADR-0003](adr/0003-native-rendition-profiles-no-object-lambda.md)).

| Profile | Format | Selects | Manifest under `assets/<asset>/<ladder>/` |
|---|---|---|---|
| `hls` | HLS | every rendition | `main.m3u8` |
| `hls-avc`, `hls-hevc`, `hls-av1` | HLS | one codec | `main_avc.m3u8`, `main_hevc.m3u8`, `main_av1.m3u8` |
| `hls-sd` | HLS | height ≤ 576 | `main_sd.m3u8` |
| `hls-hd` | HLS | 720 ≤ height ≤ 1080 | `main_hd.m3u8` |
| `hls-uhd` | HLS | height ≥ 1440 (only ladders that have it, e.g. `uhd`) | `main_uhd.m3u8` |
| `dash-avc`, `dash-hevc`, `dash-av1` | DASH | one codec | `main_avc.mpd`, `main_hevc.mpd`, `main_av1.mpd` |

`hls-avc` and `dash-avc` share the `main_avc` stem because MediaConvert writes an HLS and a DASH manifest for each
additional manifest. The combined `main.mpd` is never used: MediaConvert puts every codec in one AdaptationSet, which
DASH players handle poorly, so DASH is always per codec.

### From profiles to channel outputs

1. `channel_assembly.package_profiles` (default `hls`, `hls-avc`, `hls-hevc`, `hls-av1`, `dash-avc`) is registered on
   **every** VOD source and slate as one `HttpPackageConfiguration` each, with source group = profile name.
2. Each channel's `outputs` is a subset of `package_profiles`; an output's `ManifestName` and `SourceGroup` are the
   profile name. HLS outputs use `DATERANGE` ad markers and DASH outputs a multi-period MPD, both with a 60 s window.
3. Playback URLs end in the output name: `…/v1/master/<hash>/<channel>/hls-avc.m3u8`, `…/v1/dash/<hash>/<channel>/dash-avc.mpd`.

Channel Assembly allows **5 package configurations per VOD source** and **5 outputs per channel** by default (both
adjustable through Service Quotas), so `package_profiles` is limited to five. Because every encode already wrote every
applicable manifest, enabling another profile later (say `hls-sd`) needs no re-encode: raise the quota if needed, add
it to `package_profiles`, and re-register existing VOD sources (`origins___update_package_configurations`, or ask the
agent). A profile that is *new to the catalogue* does need a re-encode.

## Frame rates, interlace and HDR

- Every output is normalised to the ladder's frame rate (`FramerateControl: SPECIFIED`, `DUPLICATE_DROP`), so GOPs,
  segments and slates share one frame-exact grid. At 29.97 fps a GOP is exactly 2002 ms and a 15-GOP slate exactly
  30030 ms; durations never drift over a long schedule.
- Frame rates belong to a display family: **50 Hz** (12.5, 25, 50 fps) or **60 Hz** (24, 30, 60 fps and their
  ×1000/1001 rates such as 23.976 and 29.97). Conversion inside a family is allowed and recorded as a warning on the
  asset (for example 24 → 29.97 for the sample films). **Conversion across families is refused** (it judders) unless
  the ladder sets `allow_frame_rate_conversion = true`. For 25 fps libraries, add a ladder with `frame_rate = "25"`
  and bind 50 Hz channels to it.
- Interlaced and telecined sources go through MediaConvert's adaptive deinterlacer; progressive frames pass untouched.
- HDR sources (PQ, HLG) are refused: both built-in ladders are SDR.
- Sources without a video or an audio track are refused (every rendition carries audio).
- Sources smaller than the top rung are upscaled; the asset records a warning.

## Cue points and the ESAM rule

Channel Assembly can only insert an ad break where **every track** has a segment boundary
([ADR-0008](adr/0008-ad-break-alignment.md)).

1. Editorial cue points (`cue_points_ms` from the CMS, a sidecar, or the v2 `AdOffsets` S3 object tag) are snapped
   to the nearest GOP boundary (2.002 s grid).
2. Points that already sit on the 6.006 s segment grid need nothing.
3. Only points between segment boundaries add an ESAM signal (SCC XML) to the job, which splits a segment there. ESAM
   makes DASH multi-period and creates short segments, which raise harmless `SEGMENT_DURATION_MISMATCH` alerts (the
   tools filter them).
4. Without cue points, the asset's break offsets are every segment boundary common to all tracks (HE-AAC audio
   trails video by up to about 150 ms, so the match tolerance is 200 ms). That is about 600 candidate offsets per
   hour; the planner picks the ones closest to the channel's break cadence.

The library stores the valid offsets per asset (`break_offsets_ms`); the planner never invents one.

## What the pipeline verifies and fixes

The `Finalize` step runs after every encode:

- **Every applicable profile manifest exists**, and every HLS multivariant has exactly the ladder's signature (ordered
  codec family, resolution, frame rate and audio group per variant). Anything else fails the ingest, because Channel
  Assembly matches variants by position.
- **AV1 `av1C` patch.** MediaConvert writes every AV1 init segment with level 2.0 and 8-bit in the `av1C` box,
  regardless of the rendition. Finalize rewrites the level (computed from resolution and frame rate per AV1 Annex A)
  and the bit depth so that the box matches the `CODECS` attribute (for example `av01.0.08M.10`). Patched inits
  decode with ffmpeg and report the right level.
- **Nominal BANDWIDTH.** Multivariant `BANDWIDTH` and `AVERAGE-BANDWIDTH` are rewritten to the ladder's nominal
  values (formula above). Measured values of a static slate or house ad would otherwise advertise a fraction of the
  content bitrate, and Channel Assembly copies them into the channel.
- **I-frame playlists** are raised to `EXT-X-VERSION:5` when a ladder enables them.
- **Duration and break offsets** are computed from the media playlists.
- **Ads** get an SGAI-ready copy under `ads/<ladder>/<creative_id>/`: media playlists with absolute segment and
  `EXT-X-MAP` URIs on the content host, and an MPD with an absolute `BaseURL` (MediaTailor re-hosts only the manifests
  of preconditioned ads; see [ad-insertion.md](ad-insertion.md#preconditioned-ads)).

Content and slates are then registered as Channel Assembly VOD sources named `<asset_id>-<ladder key>` with one
package configuration per `package_profiles` entry; ads are not registered.

## Ladder kits

A channel needs more than programs: a filler slate for schedule gaps, an ad-break slate per break length (a Channel
Assembly break lasts exactly as long as its slate) and ad creatives encoded on the same ladder. The
`LadderProvisioner` state machine encodes this **kit** for each ladder version, from the 1920×1080 stills in
`assets/brand/` (MediaConvert's video generator renders a still for an exact number of GOPs):

| Kit item | Length | Use |
|---|---|---|
| Filler slate | 15 GOPs (30.03 s) | `FillerSlate` of every channel on the ladder; loops through gaps |
| Break slates | 15, 30, 45, 60 GOPs (30.03, 60.06, 90.09, 120.12 s) | `AdBreaks[].Slate`; fixes the break length, shows when an avail is not filled |
| House ads | 7 and 15 GOPs (14.01 and 30.03 s) | Preconditioned creatives of the static VAST pod |
| VAST pod | | `ads/<ladder>/vast.xml` (`max-age=300`) and the beacon `ads/b.gif` |

At deploy time the `LadderKit` custom resource runs the provisioner for the default ladder and every ladder an IaC
channel uses (other built-in ladders get their job template only). It is idempotent by a
fingerprint of the ladder, the brand stills, the content host, the package profiles and the source location, so
redeploys take seconds. At runtime `encoding___provision_ladder` runs the same state machine for agent-proposed
ladders. A kit costs about **$3.56** on `standard` and **$8.63** on `uhd`.

## Cost

Estimates come from `fast_channels.core.cost`: us-east-1 on-demand list prices of the first pricing tier, billed per
output, per started second, with a 10 s minimum. They are what the tools report (`library___ingest`,
`encoding___propose_ladder`); your bill is authoritative.

| Ladder | Tier | Per content minute | Per content hour | Kit (once per ladder version) |
|---|---|---|---|---|
| `standard` | Professional | $0.56 | $33.60 | $3.56 |
| `uhd` | Professional | $1.36 | $81.52 | $8.63 |

Where the money goes, per content minute:

| Ladder | AVC | HEVC | AV1 | Audio (incl. normalisation) |
|---|---|---|---|---|
| `standard` | $0.144 | $0.144 | $0.252 | $0.020 |
| `uhd` | $0.144 | $0.432 | $0.756 | $0.027 |

Rates per output minute at up to 30 fps (more than 30 fps: ×1.25):

| Codec / tier | SD (< 720 lines) | HD (720–1080) | 4K (> 1080) |
|---|---|---|---|
| AVC, Basic | $0.0075 | $0.015 | $0.030 |
| AVC, Professional | $0.012 | $0.024 | $0.048 |
| HEVC, Professional | $0.024 | $0.048 | $0.096 |
| AV1, Professional | $0.042 | $0.084 | $0.168 |
| Audio | Basic $0.003, Professional $0.00468, plus $0.002 per output for loudness normalisation | | |

A ladder is **Basic** only when it is AVC-only, without loudness normalisation and with acceleration `DISABLED`;
anything else (HEVC, AV1, normalisation, accelerated transcoding) is **Professional**. To cut cost, drop codecs your
audience does not use: an AVC-only ladder from `standard` costs $0.164 per minute on the Professional tier (the
agent's `propose_ladder` reports the exact figure).

## Customising ladders

### With the agent

The agent can derive a ladder from an existing version, provision it and build a channel on it. Every step that
creates resources or costs money needs approval.

1. **Propose.** `encoding___propose_ladder` takes a base version and operations: keep only some `codecs`, cap
   `max_height`, floor `min_height`, `quality_shift` QVBR by −2 to +2, `drop_rungs` by name, and optionally a
   `new_id`. It changes nothing; it returns the diff, validation errors and warnings, the cost per minute next to the
   base ladder's, the kit cost, and a `proposal_id` valid for 24 hours. Operations only remove or shift renditions;
   new renditions need a TOML file.
2. **Provision** (approval). `encoding___provision_ladder` creates the MediaConvert job template (category
   `<prefix>`, tagged `fast:owner=agent`) and encodes the kit. The version becomes `READY` in a few minutes.
3. **Re-encode** (approval). `library___ingest` with the new `ladder_version` encodes each title the channel needs.
4. **Create a channel** (approval). `channels___create_channel` builds an agent-owned `<prefix>-agent-<name>`
   channel on the ready ladder, with its filler slate, playback configuration, logs and SigV4 channel policy. Then plan,
   publish and start it as usual.

For example: *"Propose an AVC-only ladder up to 720p from standard for our legacy set-top boxes, then provision it,
re-encode the kids titles with it and create a channel called stb."*

IaC channels (`channels.iac`) are bound to built-in ladders only. Runtime job templates and kits are deleted by the
reaper when the stack is deleted.

### In code

1. Add `core/src/fast_channels/core/ladders/<name>.toml` (visible as `ladders/<name>.toml`).
2. Check it: `uv run python -c "from fast_channels.core.ladder import Ladder; l = Ladder.builtin('<name>'); print(l.version, l.problems())"`.
3. Add it to `ladders.builtin` in `cdk.json` and point channels at it with `"ladder": "<name>"`.
4. `make deploy` creates the job template and encodes the kit; then ingest titles with the new version.

Changing a built-in ladder's file changes its version: existing assets keep the old version and must be re-encoded
before a channel can switch.

## Codec caveats

- **Player support.** The `hls` output lists AVC, HEVC and AV1 together and lets the player choose what it can decode.
  Devices that mis-handle mixed-codec multivariants get the single-codec outputs (`hls-avc`, `hls-hevc`, `hls-av1`).
  AV1 decode generally needs hardware support. The console probes MSE support per codec.
- **HEVC** is written as `hvc1` in fMP4 (Apple requires it; TS with HEVC is unsupported by MediaTailor).
- **AV1** uses QVBR only, has no HRD or closed-GOP controls, and honours ESAM splits only on the GOP grid, which is
  why every cue point is snapped first.
- **Accelerated transcoding** applies to long jobs; video-generator inputs (kit slates and house ads) never accelerate.
  Acceleration combined with the deinterlacer has not been verified on long mezzanines.
- **Untagged sources** raise MediaConvert colour-space warnings 230001/230005. They are harmless.
- **Captions** are not part of v3.0 ([ADR-0013](adr/0013-captions-roadmap.md)).
