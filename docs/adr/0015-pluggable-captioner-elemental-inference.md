# 0015 — Missing captions come from a pluggable captioner; AWS Elemental Inference smart subtitles is the engine

**Status:** Accepted

## Context
Generated captions use AWS Elemental Inference smart subtitles. In October 2026 it is a live service: MediaLive
integrates it natively, and MediaConvert's integration offers only smart crop. Live experiments showed that a file
can be captioned through a feed's `PutMedia` / `GetMetadata` data plane, within these limits:

- 1.5 PutMedia requests per second per stream, not adjustable: about 1.4x real time per feed; 2 active feeds per
  account by default (adjustable); `ServiceQuotaExceededException` when none is free.
- 1280x720 H.264 video is mandatory (its picture does not matter), with AAC audio, in CMAF Ingest segments of at
  most 1.2 s with one `moof` each.
- The transcriber is a stream: a delivery gap of 1.5 s or more loses speech, a fresh feed often loses its first
  seconds, and the last ones need a flush.
- Results are live roll-up snapshots (a growing two-line display, one `<p>` per word) in TTML windows of at most
  30 s, available within about 20 s of the last segment.
- $0.15 per minute.

## Decision
Generation is a contract, not a service: a state machine that takes the asset, the language, the mezzanine and an
output stem, and writes a caption file (`functions.ingest.captions`). The ingest pipeline runs the stage's captioner
with `startExecution.sync:2`; a failure leaves the track empty.

The bundled `InferenceCaptioner` handles the limits explicitly:

- a MediaConvert job on its own queue writes the proxy: 1 s CMAF segments of 1280x720 H.264 and AAC, with 8 s of
  generated silence before the programme and 10 s after it;
- the proxy is split into 10-minute chunks with 15 s of overlap on each side, each relayed by one Lambda invocation
  to a fresh feed at a steady 1.4x, the last segment marked `lmsg`, then read back window by window;
- a Map runs `captions.max_feeds` chunks at once, retrying with backoff while the account's feeds are in use;
- each chunk keeps the words that start in its own seconds; the stitch step turns the roll-up into timed words and
  pop-on captions (two lines of 37 characters, broken at sentences and pauses) and writes one IMSC1 TTML file;
- feeds are tagged with the deployment and the function may use no other feeds; a failed execution deletes its
  feeds;
- the functions bundle ships its locked boto3: the Lambda runtime's SDK knew Elemental Inference without smart
  subtitles (`Unknown parameter in outputs[0].outputConfig: "subtitling"`, October 2026).

## Consequences
A title takes about 0.7 minutes of relay per minute of content divided by `max_feeds`, plus the proxy job: a 2-hour
film with 2 feeds is captioned in about 50 minutes. Captions cost $0.15 per minute plus about $0.03 per minute of
proxy. They are ASR quality, without sound or music descriptions or speaker labels, recorded as `generated`, and kept
under `captions/` of the ingest bucket for correction; a custom dictionary (`captions.dictionary_id`) improves names.
Customers can plug in another engine (a captioning vendor, a review workflow) without touching the pipeline. When
MediaConvert offers smart subtitles for files, it becomes another captioner, or part of the caption scan.
