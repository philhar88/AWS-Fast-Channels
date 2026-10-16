# Channel Assembly: origins, channels and schedules

AWS Elemental MediaTailor Channel Assembly plays VOD sources back to back as a linear channel, without a live
encoder. FAST Channels uses it with an S3 SigV4 origin, LINEAR channels with a filler slate, and schedules that the
agent's tools compute and publish. This page covers the origin, VOD sources and source groups, channels and their
lifecycle, programs and the EPG, quotas, and bringing your own pre-encoded content.

- [Source location: the media bucket over SigV4](#source-location-the-media-bucket-over-sigv4)
- [VOD sources and source groups](#vod-sources-and-source-groups)
- [Channels](#channels)
- [Slates](#slates)
- [Programs and ad breaks](#programs-and-ad-breaks)
- [The schedule of record and the EPG](#the-schedule-of-record-and-the-epg)
- [Alerts](#alerts)
- [Quotas and rate limits](#quotas-and-rate-limits)
- [Bring your own source locations](#bring-your-own-source-locations)

## Source location: the media bucket over SigV4

`ChannelAssemblyOrigin` registers the media bucket as one source location (stack output `SourceLocationName`):

| Setting | Value | Why |
|---|---|---|
| `AccessConfiguration.AccessType` | `S3_SIGV4` | The bucket stays private; MediaTailor signs its reads |
| `HttpConfiguration.BaseUrl` | `https://<media bucket>.s3.<region>.amazonaws.com` | SigV4 needs the regional virtual-hosted URL; path-style, global and dual-stack hosts are rejected |
| `DefaultSegmentDeliveryConfiguration.BaseUrl` | the content base URL (`delivery.content_domain`, or the CloudFront domain) | Channel manifests carry absolute segment URLs on this host; ad insertion does not rewrite them |

The Data stack's bucket policy admits `mediatailor.amazonaws.com` to `s3:GetObject` on `assets/*` only for this
source location, `<prefix>-media` in this account (`aws:SourceAccount` and `aws:SourceArn`), so other deployments'
source locations cannot read it. The bucket must use SSE-S3:
Channel Assembly cannot read SSE-KMS objects. `CreateVodSource` also checks that the *caller* can read the manifests,
which is why the pipeline's Register step and the origins tools have `s3:GetObject` on `assets/*`.

## VOD sources and source groups

Every content asset and slate becomes one VOD source, named `<asset_id>-<ladder key>` (for example
`my-movie-3f2a1c-standard_86bf8088e0`), with one HTTP package configuration per entry of
`channel_assembly.package_profiles`:

| Source group | Type | Path (relative to the source location) |
|---|---|---|
| `hls` | HLS | `/assets/<asset>/<ladder>/main.m3u8` |
| `hls-avc` | HLS | `/assets/<asset>/<ladder>/main_avc.m3u8` |
| `hls-hevc` | HLS | `/assets/<asset>/<ladder>/main_hevc.m3u8` |
| `hls-av1` | HLS | `/assets/<asset>/<ladder>/main_av1.m3u8` |
| `dash-avc` | DASH | `/assets/<asset>/<ladder>/main_avc.mpd` |

A channel output reads the source group of the same name. Channel Assembly requires every program, filler slate and
ad slate of a channel to serve every output's source group with identical renditions, which the shared ladder
guarantees ([encoding.md](encoding.md#rendition-profiles)). Registration runs one VOD source at a time (the API
allows 1 TPS) and then checks the source's alerts. Ads are not registered: MediaTailor reads them from the VAST pod.

## Channels

### IaC channels

Channels in `cdk.json` (`channels.iac`) are created by CloudFormation with `LinearChannel`:

```json
"channels": {
  "iac": [{
    "name": "{prefix}-demo",
    "ladder": "standard",
    "outputs": ["hls", "hls-avc", "hls-hevc", "hls-av1", "dash-avc"],
    "tier": "BASIC",
    "autopilot_cron": null,
    "brief": null
  }],
  "max_runtime_hours": 4,
  "max_agent_channels": 3
}
```

`{prefix}` in a name is replaced with the deployment prefix, so the shipped channel is `fast-demo` by default and
`news-demo` with `make deploy PREFIX=news`. Each channel gets:

- a **LINEAR** channel (programs play once, back to back; gaps play the filler slate) with the ladder kit's filler
  slate, one output per profile and `AS_RUN` logging;
- its own **playback configuration** of the same name (`PLAYER_SELECT`, so SSAI and SGAI) with three vended-log
  deliveries ([ad-insertion.md](ad-insertion.md#the-playback-configuration));
- a **channel policy** that admits only `mediatailor.amazonaws.com` for exactly that playback configuration (SigV4),
  so anonymous requests to the channel get 403 ([ADR-0007](adr/0007-sigv4-channel-origin-no-cdn-shield.md));
- a `CHANNEL#<name>` item in the media library (ladder, outputs, playback prefixes, owner `iac`, brief);
- **lifecycle custom resources**: an update guard that fails fast when an update would change a `RUNNING` channel
  (the `LinearChannel` construct's `restart_on_update=True` stops, updates and restarts instead, also when a later
  failure rolls the update back), an endpoint lookup, and a delete step that stops the channel and deletes the
  programs Channel Assembly can still hold (its schedule and the library programs from a day ago on). A delete that
  runs out of time fails with the number of programs left; deleting the stack again resumes.

Names must start with `<prefix>-`; `<prefix>-agent-` is reserved for the agent.

### Agent channels

`channels___create_channel` builds the same set of resources at runtime, from the same `core.specs` functions, as
`<prefix>-agent-<name>` (name at most 24 characters), tagged `fast:owner=agent`. It needs a `READY` ladder, and at
most `channels.max_agent_channels` (default 3) may exist. Only agent channels can be deleted through the tools; Cedar
forbids destructive channel tools on any other name.

### Tiers and cost

| Tier | Price (us-east-1) | Adds |
|---|---|---|
| `BASIC` (default) | $0.10 per running channel-hour (about $73 per month 24/7) | VOD sources only |
| `STANDARD` | $0.35 per running channel-hour (about $256 per month 24/7) | Live sources, program rules and time-shift (not used by the v3.0 tools) |

A channel bills only while `RUNNING`. **No deployment ever starts a channel.** `channels___start_channel` (and the
console's Start button) starts it and creates a one-time EventBridge Scheduler schedule that stops it after
`channels.max_runtime_hours` (default 4; `null` disables auto-stop). Starting a running channel moves the stop time.

## Slates

| Slate | Length | Role |
|---|---|---|
| Filler | 15 GOPs (30.03 s) | Plays whenever the schedule has a gap, looping as needed |
| Break slates | 15, 30, 45, 60 GOPs (30.03, 60.06, 90.09, 120.12 s) | Each ad break names one; **the break lasts exactly as long as its slate** |

When MediaTailor fills an avail only partly, or not at all, viewers see the rest of the break slate. All slates come
from the ladder's [kit](encoding.md#ladder-kits) and are encoded on the channel's ladder.

## Programs and ad breaks

Programs are runtime data: the deployment creates none. The agent plans and publishes them in blocks.

**Planning** (`schedule___plan`) is pure arithmetic in `core.timeline.plan_block`:

- Program names are deterministic: `<yyyymmddThhmmss>-<asset_id>` of the planned UTC start (at most 64 characters).
- An ad break is placed at one of the asset's valid offsets ([encoding.md](encoding.md#cue-points-and-the-esam-rule))
  nearest to the brief's cadence. A break *inserts* its slate: a 10-minute program with two 60 s breaks airs for 12
  minutes. The cadence runs across program boundaries within a block.
- Break policy (in the channel brief):

  | Field | Default | Meaning |
  |---|---|---|
  | `cadence_minutes` | 12 | A break is due this long after the previous one ended (or the block started) |
  | `min_gap_minutes` | half the cadence | Never less than this from the end of one break to the next |
  | `max_breaks` | unlimited | Breaks per program |
  | `slate_gops` | 30 (about 60 s) | Break length; one of 15, 30, 45, 60 |
  | `start_margin_seconds`, `end_margin_seconds` | 60, 30 | No break at a plain segment boundary this close to a program's start or end |
  | `min_program_minutes` | 3 | Programs shorter than this break only at editorial cue points |

  Editorial cue points (the asset's `cue_points_ms`) are exempt from the margins and the minimum length: the editor
  chose them. Short-form content without cue points therefore airs without breaks; lower `min_program_minutes` (and
  the margins) to break inside it.
- The brief's `ratings`, `genres_allow`, `genres_deny` and UTC `dayparts` are checked per program.

Each break is a SCTE-35 `TIME_SIGNAL` with one segmentation descriptor: type `0x34` (provider placement opportunity
start), a deterministic 31-bit `SegmentationEventId`, `SegmentNum`/`SegmentsExpected`, and an MPU UPID (type `0x0C`)
of format identifier `FST3` plus the content id. These ids reach the ad decision server and every log
([observability.md](observability.md#correlation-contract)).

**Violations.** Errors block publishing: `START_IN_PAST`, `OVERLAP`, `NAME_CONFLICT`, `LADDER_MISMATCH`,
`MAX_PROGRAMS`, `BREAK_OFFSET`, `RATING`, `GENRE`, `DAYPART`, `EMPTY`, and from the tool `UNKNOWN_ASSET`,
`NOT_CONTENT`, `NOT_READY`. Warnings do not: `LEAD_TIME` (the block starts less than 10 minutes ahead; Channel
Assembly recommends at least 10 minutes and late inserts are best effort) and `GAP` (filler before the block).

**Publishing** (`schedule___publish`, approval required) replays the stored plan (valid for one hour) against the live
schedule:

1. Takes a per-channel lease (a conditional DynamoDB item with a TTL): Channel Assembly rejects concurrent schedule
   edits with `ConflictException`, so there is one writer per channel.
2. Re-anchors the block after the live program that precedes it *in time*. The first program is `ABSOLUTE` at its
   planned start and the rest are `RELATIVE` (`AFTER_PROGRAM`). List order beats wall-clock time for `ABSOLUTE`
   programs, so the anchor matters; a block before the whole schedule is placed `BEFORE_PROGRAM`.
3. Fails with `PLAN_STALE` when the live schedule from the anchor on changed since planning, or when re-validation
   finds new errors (including an asset that is no longer `READY`). Plan again.
4. Calls `CreateProgram` strictly in sequence at 3 TPS or less, writing each program's `PROGRAM#` item as soon as
   Channel Assembly has it. Program names are deterministic, so a retried publish skips programs that already exist
   and is idempotent.
5. Writes the EPG feeds, emits `Schedule Published` and returns filtered alerts. The event and the alerts are best
   effort: the programs are on air by then, so failing to read or announce is logged (alerts that cannot be read
   come back as one `INFO` alert, `ALERTS_UNAVAILABLE`) instead of failing the publish.

If a program cannot be created (an error, or the function nearing its timeout), publishing stops, rewrites the guide
for what was published and fails with the error's code and `published <k> of <n> programs of <plan id>`. Publishing
the same `plan_id` again adds the rest; `schedule___remove_programs` takes the published ones off instead.

An `ABSOLUTE` start snaps to the next filler segment boundary, so a block can start up to about one second late.
Start the channel before the block's first program: when a channel starts after that program's start time, Channel
Assembly skips it and airs the block's `RELATIVE` programs at once, earlier than the guide says.

`schedule___remove_programs` (approval required) removes programs that have not started. Later programs of the same
block move up, like Channel Assembly re-links them; the next block keeps its start and filler plays in between.

## The schedule of record and the EPG

Channel Assembly deletes LINEAR programs about **13 to 15 minutes after they finish airing**, even on a stopped
channel (observed, not documented). The media library's `PROGRAM#` items are therefore the schedule of record:

- Publish writes `epg/<channel>.json` (FAST Channels feed) and `epg/<channel>.xml` (XMLTV) to the media bucket with
  `Cache-Control: public, max-age=30`. Players and partners read them through the CDN at
  `<CdnBaseUrl>/epg/<channel>.json`.
- Channel Assembly writes `VodProgramStart` events to the account's as-run log group `MediaTailor/Channel/AsRunLog`.
  A subscription filter sends this deployment's events to a reconciler that marks programs `AIRED` with `aired_at`.
- `PROGRAM#` items expire 30 days after the program ends (DynamoDB TTL `expires_at`), so the history cannot grow
  without bound; the guide shows the last 6 hours.

```json
{"channel": "fast-demo", "generated_at": "2026-10-16T12:00:00Z", "ladder_version": "standard@86bf8088e0",
 "programs": [{"name": "20261016T120000-sintel-3f2a1c", "title": "Sintel", "content_id": "sintel",
   "asset_id": "sintel-3f2a1c", "start": "2026-10-16T12:00:00Z", "end": "2026-10-16T12:15:48Z",
   "rating": "TV-14", "genres": ["animation", "fantasy", "adventure"],
   "breaks": [{"offset_ms": 246246, "duration_ms": 60060}], "status": "PUBLISHED"}]}
```

The as-run log group is shared by every channel in the account and region, and a log group allows two subscription
filters, so at most two deployments per account and region get the `AIRED` reconciliation. The log group's retention
is set to the last deploying stack's `observability.log_retention_days`, and deleting a stack leaves the group.

## Alerts

`origins___list_alerts` returns a channel's, VOD source's or source location's alerts, most severe first.
`SCHEDULING_ERROR` alerts break playback and must be fixed. `SEGMENT_DURATION_MISMATCH` and `BANDWIDTH_MISMATCH` are
expected with ESAM-split content and nominal BANDWIDTH and are hidden unless `include_noise` is set; they would
otherwise bury real alerts (Channel Assembly stops at about 50 per program). `schedule___publish` and
`channels___start_channel` return the filtered alerts too.

## Quotas and rate limits

Account defaults per region (adjustable through Service Quotas unless noted):

| Quota | Default | Where it bites |
|---|---|---|
| Programs per channel | 400 | About 8 days of 30-minute programs. Aired programs are deleted automatically |
| Channels per account | 100 | IaC plus agent channels across deployments |
| Outputs per channel | 5 | `outputs` |
| Package configurations per source | 5 | `channel_assembly.package_profiles` |
| VOD sources per source location | 1,000 | Titles × ladder versions, plus 5 slates per kit |
| Source locations | 100 | |
| Segment delivery configurations per source location | 5 | |
| Channel manifest requests | 50 TPS per channel, 400 TPS per account | SSAI audience size; see [capacity](delivery-and-multi-cdn.md#capacity) |
| Manifest size | 2 MB (fixed) | |

API rates: 1 TPS for `CreateChannel`, `CreateSourceLocation`, `CreateVodSource`, `UpdateVodSource`,
`DeleteVodSource`, `StartChannel`/`StopChannel`; 3 TPS for `CreateProgram`, `DeleteProgram`, `PutChannelPolicy`;
5 TPS for `Describe*`, `List*` and `GetChannelSchedule`. The pipeline, tools and reaper pace themselves and retry
with backoff.

## Bring your own source locations

Content that is already encoded can be scheduled without a re-encode, as long as it matches a ladder exactly. The
`origins` tools do this through the agent:

1. **`origins___create_source_location`** (approval). Creates an `S3_SIGV4` source location
   `<prefix>-agent-<name>` for a bucket in the same account and region, and returns the two bucket-policy
   statements to apply: read for MediaTailor scoped to that source location, and read for the tools' role
   (`CreateVodSource` checks the caller). The agent never edits bucket policies; it shows the statements verbatim. The
   media bucket and bucket names containing dots are refused.
2. **`origins___register_vod_source`**. Reads each HLS multivariant of the given packages and requires its signature
   (codec, resolution, frame rate and audio group of every variant, in order) to equal the ladder's; otherwise it
   returns `LADDER_MISMATCH` naming the first difference. It computes the duration and the common segment boundaries,
   creates (or adopts) the VOD source `<prefix>-agent-<asset>-<fingerprint>` and adds a library asset (`owner` agent)
   that can be scheduled like any other.
3. **`origins___update_package_configurations`** (approval). Replaces the source groups of a VOD source (at most
   five), for example to add `hls-sd` before a channel gets that output.

Requirements: SSE-S3 objects, relative child URIs in the multivariant playlists, and a 1:1 match with the channel's
ladder, including audio groups. DASH manifests are only checked for being an MPD. Agent source locations and their
VOD sources are deleted by the reaper when the stack is deleted.
