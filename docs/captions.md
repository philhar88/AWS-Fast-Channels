# Captions: closed captions on every channel

Every asset of a captioned ladder carries the ladder's caption tracks, IMSC1 text in the same CMAF encode as its
video and audio, through Channel Assembly, MediaTailor SSAI and SGAI, and the CDN. One setting turns captions on
for a deployment; the pipeline then finds each asset's captions itself: a caption file that came with it, captions
already in the source, or, when there are none, captions it generates from the main audio.

- [Turn captions on](#turn-captions-on)
- [Where an asset's captions come from](#where-an-assets-captions-come-from)
- [Providing caption files](#providing-caption-files)
- [Captions in the source](#captions-in-the-source)
- [Generated captions and the captioner contract](#generated-captions-and-the-captioner-contract)
- [Why IMSC1, and why caption tracks belong to the ladder](#why-imsc1-and-why-caption-tracks-belong-to-the-ladder)
- [Through Channel Assembly, ad insertion and players](#through-channel-assembly-ad-insertion-and-players)
- [Cost](#cost)
- [Limits](#limits)

## Turn captions on

```jsonc
// cdk.json, "fast-channels"
"captions": {
  "enabled": true,                  // the built-in ladders' [[captions]] tracks are encoded; false: no tracks
  "required": false,                // true: refuse an asset whose spoken-language track would be empty
  "engine": "elemental-inference",  // who generates missing captions: elemental-inference or none
  "max_feeds": 2,                   // Elemental Inference feeds used at once (the account quota is 2 by default)
  "dictionary_id": null             // an Elemental Inference custom dictionary for names and jargon
}
```

The tracks themselves are part of each ladder (`ladders/standard.toml`, `ladders/uhd.toml`):

```toml
[[captions]]
language = "en"         # BCP-47: HLS LANGUAGE, DASH @lang
label = "English"       # HLS NAME, DASH Label (letters, digits, spaces)
kind = "captions"       # captions (closed captions, SDH: CHARACTERISTICS transcribes-spoken-dialog and
                        # describes-music-and-sound) or subtitles (dialogue only)
```

Add a `[[captions]]` table per language. Tracks are in the ladder's fingerprint, so a change is a new ladder version
with its own kit (a ladder without captions keeps the version it had before ladders could carry them:
`standard@86bf8088e0`). The agent can propose them too: `encoding___propose_ladder` with
`ops.captions = [{"language": "en", "label": "English"}, {"language": "es", "label": "Spanish"}]`.

## Where an asset's captions come from

For each caption track of the ladder, the first of these that applies is used
(`fast_channels.core.captions.plan_captions`):

| # | Source | How it is found | Recorded as |
|---|---|---|---|
| 1 | A **provided file** in that language | `captions.files` of the ingest request, or a drop-folder sibling `<stem>.<language>.<ext>` | `provided` |
| 2 | **Captions in the source**, for the spoken language | a QuickTime `c608` data track (MediaConvert Probe), or CEA-608/708 in the video (the caption scan) | `embedded` |
| 3 | **Generated**, for the spoken language | the stage's captioner (Elemental Inference smart subtitles) transcribes the main audio | `generated` (with the engine) |
| 4 | Nothing | an empty track, so every asset of the ladder has the same tracks | `none` |

The *spoken language* is `captions.spoken_language` of the request, else the language of the source's first audio
track, else the ladder's first caption track. Only that track can come from the source or be generated; other
languages need a file. With `required: true`, an asset whose spoken-language track would be empty is refused, as
is one whose spoken language has no track in the ladder.

`captions.mode` overrides the order per asset: `auto` (the table), `generate` (ignore captions in the source and
generate the spoken language) or `off` (empty tracks; refused when captions are required).

The ingest state machine shows the steps: `Prepare` resolves provided files and Probe-visible tracks; when the
spoken language still has no source it runs `ScanCaptions` (a MediaConvert job) and `CaptionsScanned` (in `generate`
mode it skips the scan), then, if the scan found nothing, `GenerateCaptions` (the captioner) and `CaptionsGenerated`
(or `CaptionGenerationFailed`, which leaves the track empty unless captions are required); `Transcode` then reads every track through the job's caption
selectors. Each asset records where every track came from: the media library item and `library___get_asset`
(`captions`), the console's library table, and the `Asset Ready` event.

## Providing caption files

| Format | Extensions | Notes |
|---|---|---|
| SCC | `.scc` | Timecodes are shifted to the zero-based source: minus the source's start timecode when the file starts at or after it, else minus the whole hours of its first cue (`01:00:00;00` -> `-3600000` ms). Set `offset_ms` to override, `frame_rate` for non-29.97 files. CEA-608 positions are kept. |
| SRT | `.srt` | Plain text only: MediaConvert shows `<font>`, `<b>`, `<i>`, `<u>` as text; the ingest warns. |
| WebVTT | `.vtt`, `.webvtt` | |
| TTML, DFXP, EBU-TT-D | `.ttml`, `.dfxp`, `.xml` | Regions and styles are kept. |
| SMPTE-TT, IMSC | `.xml` | Declare `"format": "smpte-tt"` or `"imsc"` (an `.xml` file is read as TTML otherwise). |
| EBU STL, SAMI | `.stl`, `.smi`, `.sami` | |

**Drop folder.** Upload the caption files, then the JSON sidecar if any, then the video. Files named
`<stem>.<language>.<ext>` next to the video are its captions (`movie.en.scc`, `movie.pt-BR.srt` for `movie.mov`),
unless the sidecar lists files itself.

**Ingest request** (sidecar JSON, `Ingest Requested` event, or the agent's `library___ingest`):

```json
{
  "captions": {
    "mode": "auto",
    "spoken_language": "en",
    "files": [
      {"uri": "movie.en.scc", "language": "en"},
      {"uri": "s3://my-ingest-bucket/subtitles/movie-es.ttml", "language": "es"},
      {"uri": "https://cms.example.com/captions/movie.fr.vtt", "language": "fr", "offset_ms": -2000}
    ]
  }
}
```

A relative `uri` is relative to the video's folder. `s3://` files must be in the ingest bucket (the encoder role
reads only that bucket); `https://` files are fetched by MediaConvert. Prepare refuses a file that is missing,
empty, or elsewhere. At most one file per language; a file whose language matches a track by its primary subtag
(`en-US` for an `en` track) fills that track.

## Captions in the source

MediaConvert reads CEA-608/708 carried in AVC/HEVC SEI, MPEG-2 user data and MXF VANC, and QuickTime `c608`
tracks. Probe reports only the last kind (a data track with codec `C608`), so for every other source the pipeline
runs a **caption scan**: a MediaConvert job (no template) whose `EMBEDDED` selector writes CC1 as a TTML file. A
scan that finds no captions completes with a TTML without cues; cues mean the encode reads the source's captions
directly (`EMBEDDED`, CC1, row positions kept). A caption output needs a video output in its group, so the scan
also writes a 256x144 picture that nothing reads. Both files live under `work/<asset>/<correlation id>/` of the
ingest bucket, which expires after 7 days.

Not read: Matroska text tracks, DVB subtitles (bitmaps), and `tx3g` (MP4 timed text, whose cue end times MediaConvert
stretches). Extract those to a file and provide it.

## Generated captions and the captioner contract

When neither a file nor the source has the spoken language's captions, the pipeline starts the stage's
**captioner**, a state machine, with `startExecution.sync:2`:

```jsonc
// input
{"asset_id": "...", "language": "en", "correlation_id": "...",
 "source_uri": "s3://ingest/films/movie.mov",                      // the mezzanine
 "output_uri": "s3://ingest/captions/<asset>/<correlation id>/en"} // the stem to write to
// output
{"uri": "s3://ingest/captions/<asset>/<correlation id>/en.ttml", "format": "ttml", "engine": "elemental-inference"}
```

Any state machine with this input and output plugs in (`IngestPipeline(captioner=...)` takes anything with an
`engine` name and a `state_machine`): a captioning vendor behind a callback, a human review workflow, or another
engine. A failure leaves the track empty (or refuses the asset when captions are required). Generated files stay
under `captions/` of the ingest bucket: correct one and ingest again with it as a provided file.

**AWS Elemental Inference smart subtitles** (`InferenceCaptioner`, [ADR-0015](adr/0015-pluggable-captioner-elemental-inference.md))
is a live feature: a *feed* transcribes CMAF Ingest segments a client pushes with `PutMedia` and answers TTML for
any window of up to 30 s with `GetMetadata`. For a file, the captioner:

1. writes a **proxy** with MediaConvert, on its own queue: 1 s CMAF segments of 1280x720 H.264 (the only size a feed
   accepts; the picture does not matter) and AAC-LC stereo 48 kHz, with 8 s of generated silence before the
   programme (a fresh feed loses its first seconds) and 10 s after it (flushes the transcriber);
2. splits it into **chunks** of 10 minutes with 15 s of context on each side (`core.inference.plan_chunks`);
3. **relays** each chunk to a fresh feed in one Lambda invocation: the init segments, then video and audio segment
   `n` every 0.7 s (about 1.4x real time; PutMedia allows 1.5 requests per second per stream, and any gap of 1.5 s
   loses speech), the last segment marked `lmsg`; then, 20 s later, reads every window of the chunk and keeps the
   words that start in its own seconds. `max_feeds` chunks run at once; a chunk that finds the account's active
   feeds in use is retried with backoff. The feed is deleted after its chunk, and a failed execution deletes all of
   its feeds;
4. **stitches** the chunks: the live roll-up (a two-line display that grows one word per `<p>` and scrolls) becomes
   timed words (`core.inference.words`) and pop-on captions of at most two lines of 37 characters, broken at sentence
   ends, at pauses of 1.5 s and after 6 s (`core.inference.captions`), written as one IMSC1 TTML file on the source
   timeline. A title without speech fails with `NoSpeechError`, so its track stays empty.

A 10-minute chunk takes about 8 minutes, so a 2-hour film is captioned in about 50 minutes with 2 feeds (raise the
account's `Active feeds per account` quota and `max_feeds` to go faster). Smart subtitles transcribes English (US,
GB, AU), French, German, Italian, Portuguese and Spanish; another track language fails with
`UnsupportedLanguageError`. Transcripts are ASR: names and jargon come out phonetically ("Thom" as "Tom"), and there
are no sound or music descriptions or speaker labels, which is why every generated track is recorded as
`generated`. Review them where regulation requires accuracy; a
[custom dictionary](https://docs.aws.amazon.com/elemental-inference/latest/userguide/create-feed-outputs.html)
(`captions.dictionary_id`) fixes recurring names.

## Why IMSC1, and why caption tracks belong to the ladder

- **IMSC1 text** (`stpp.ttml.im1t`) is the only caption output MediaConvert writes in CMAF segmented on the video
  grid: the same 6.006 s segments as video (after an ESAM split, once Finalize re-cuts it; see *Same segments*
  below), so every ad-break offset is a boundary of every track, and one track
  serves HLS (`EXT-X-MEDIA TYPE=SUBTITLES`) and DASH (an `application/mp4` `stpp` AdaptationSet). Each track is its
  own CMAF track file (`main_cc-en.m3u8`, `main_cc-eninit.cmft` with one `stpp` sample entry and `.cmft` segments);
  no video or audio track carries caption data, as CMAF requires one track per file. It is a profile of
  TTML close to SMPTE-TT, the safe-harbour interchange format of the US IP-captioning rules
  ([47 CFR §79.4](https://www.ecfr.gov/current/title-47/chapter-I/subchapter-C/part-79/subpart-A/section-79.4)).
- **WebVTT** is never delivered (provided WebVTT files are read into IMSC1): MediaConvert's CMAF WebVTT is one
  unsegmented file per track, so Channel Assembly finds no common segment boundary, drops every ad break, and refuses
  the DASH manifest.
- **CEA-608/708 embedded** in video cannot be written into CMAF by MediaConvert (any codec), and AV1 can never carry
  it. Outlets that require 608 need a separate TS output, which this reference does not include.
- **Parity.** Channel Assembly matches subtitle renditions by position. A program, filler or break slate with
  different caption tracks than the one before it makes Channel Assembly drop the ad break without an alert, answer
  404 for the subtitle playlist, or reset its sequence numbers. So the tracks are part of the ladder: slates and
  house ads are encoded with empty tracks (`NULL_SOURCE`), uncaptioned titles too, and Finalize and
  `origins___register_vod_source` refuse an encode whose subtitle renditions differ from the ladder's
  (`core.manifests.ladder_signature` lists them after the variants).
- **Same segments.** Channel Assembly and MediaTailor number every track's segments separately. At an ESAM split
  (an off-grid cue point) MediaConvert cuts the caption track like the video, but then returns to the plain 6.006 s
  grid while the video carries on from the split: Tears of Steel came out with 124 video and 125 caption segments.
  Each airing then moved the subtitle media sequence one further from the video's, MediaTailor turned that into a
  different discontinuity sequence, and players timed captions with another discontinuity's timestamps or stopped
  loading them. Finalize re-cuts such a track on the video's segments (`core.imsc`, `core.mp4.write_fragment`): the
  video's durations and count, every cue clipped to its new segments (pieces of a cue split at the old boundaries
  joined first), MediaConvert's boxes and document head kept, and the media playlist and the DASH text timelines
  copied from the video's. Encodes without ESAM are left as they are.
- **What an offset does in SSAI.** MediaTailor places every break at the video's media sequence number in every
  rendition. With a subtitle offset of N, the subtitle playlist's break starts N segments early, replacing content
  entries already served, and ends N segments late; around breaks every rendition's
  `EXT-X-DISCONTINUITY-SEQUENCE` changes between reloads (mediastreamvalidator 652018 and 652014, Critical;
  536001-536004 and 552027). HLS lets every rendition number its segments on its own (HLS 2nd edition §4.4.3.2), so
  this is a MediaTailor defect that our tracks set off. Channel Assembly caches source playlists
  ([manifest caching](https://docs.aws.amazon.com/mediatailor/latest/ug/channel-assembly-manifest-caching.html)): a
  track rewritten after registration keeps airing as it was until `UpdateVodSource`, which the pipeline's Register
  step calls for an existing source. An offset a channel has built up stays until the channel restarts.

## Through Channel Assembly, ad insertion and players

| Path | What viewers get |
|---|---|
| Channel Assembly | The tracks pass through untouched, renamed by position (`sub_1.m3u8`), on the video segments' timeline. |
| SSAI HLS | During an ad MediaTailor serves one blank WebVTT (`/v1/webvtt/.../web.vtt`) in every subtitle rendition, the IMSC1 one included, so that playlist is not valid HLS during ads (see [Limits](#limits)); content captions resume after the break. |
| SGAI (HLS Interstitials) | The primary stream keeps the slate's (empty) track under the interstitial; the house ad's own multivariant lists the same tracks. |
| SSAI DASH | Every period, ad periods included, has the `stpp` AdaptationSet: house ads carry the empty track. |
| Console | Both players show a CC toggle (on by default) when the stream has caption tracks; hls.js renders IMSC1 as text tracks, until one of the hls.js bugs below stops them. |

hls.js 1.7.3 (and its canary in October 2026) has three subtitle bugs that stop caption display on a live channel
for the rest of the session. The console uses stock hls.js, so its players show them until hls.js fixes them:

- A subtitle fragment that fails to parse is never buffered, so the subtitle controller waits on it. SSAI's blank
  WebVTT ad segments in the IMSC1 rendition fail to parse at every break.
- An IMSC1 fragment that arrives before the video's timestamps of its discontinuity throws (`initPTS.baseTime` of
  undefined), at programme and slate boundaries. hls.js defers WebVTT fragments in that case, but not IMSC1.
- After an SGAI interstitial hands the media element back, the primary player has lost its record of buffered
  subtitles (only new subtitle tracks recreate it), so the first caption fragment after every break is never buffered.

The deployed suite checks all of this (`tests/e2e`: the provided track, its grid and its separate CMAF track,
subtitle continuity through SSAI breaks, SGAI ad tracks, DASH text periods, and a speaking clip captioned by
Elemental Inference).

## Cost

- Caption outputs are not billed by MediaConvert (0 s metered), but captions need the Professional tier, which every
  ladder with HEVC, AV1, loudness normalisation or acceleration uses anyway.
- A caption scan bills one small AVC output for the asset's duration (about $0.012 per minute of content), only for
  assets whose captions were neither provided nor in a QuickTime track.
- Generated captions: Elemental Inference smart subtitles at $0.15 per minute of proxy (the programme plus 18 s of
  silence, plus 30 s of overlap per extra chunk), and its proxy job (an HD AVC and an AAC output, about $0.03 per
  minute), only for assets that need them.

## Limits

- One track per language and ladder; no forced subtitles (`FORCED=YES` needs a manifest rewrite MediaConvert does not
  do).
- Only the spoken language is taken from the source or generated, and only CC1 of embedded captions.
- IMSC1 needs a player that renders it: hls.js, AVPlayer (iOS 11+, tvOS), ExoPlayer, dash.js and Shaka do; some
  smart-TV stacks render only WebVTT or 608.
- During SSAI HLS ads MediaTailor puts its blank WebVTT into the IMSC1 rendition, although it has matched the house
  ad's own caption playlist to that rendition (`FILLED_AVAIL` in the vended logs lists `sub_1.m3u8` ->
  `main_cc-en.m3u8` under `adPlaylistUris`). The content's `EXT-X-MAP` (an IMSC1 init segment) still applies to those
  WebVTT segments, since an `EXT-X-MAP` applies to every later segment until the next one (HLS 2nd edition
  §4.4.4.5), so one rendition mixes two segment formats. Apple's mediastreamvalidator reports every blank segment as
  *No samples found in the segment* (641087, Critical), and *Each fMP4 Segment in a Media Playlist MUST have an
  EXT-X-MAP tag* (535008) when the playlist window starts inside a break. MediaTailor documents the blank WebVTT for
  WebVTT renditions only ([dynamic transcoding](https://docs.aws.amazon.com/mediatailor/latest/ug/dynamic-transcoding.html))
  and has no setting for subtitle renditions, so this is MediaTailor's behaviour, not a channel setting. An ad's own
  captions never show in SSAI, and players have to survive segments they cannot parse (hls.js 1.7.3 stops
  loading subtitles at the first one, see above); SGAI is not affected.
