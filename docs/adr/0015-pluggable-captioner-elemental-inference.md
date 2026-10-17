# 0015 — Missing captions come from a pluggable captioner; AWS Elemental Inference smart subtitles is the engine

**Status:** Superseded by [0016](0016-plain-hls-ts-avc-aac-basic-tier.md)

## Context
Titles without caption files or embedded captions needed generated captions. AWS Elemental Inference smart subtitles
is a live service; a file can be captioned only by relaying it to a feed at about 1.4x real time.

## Decision
A captioner state-machine contract, with a bundled engine that relayed a MediaConvert proxy of the file to Elemental
Inference in parallel 10-minute chunks and wrote pop-on IMSC1 captions.

## Consequences
[0016](0016-plain-hls-ts-avc-aac-basic-tier.md) removed captions, and with them the captioner, its proxy queue and its
configuration.
