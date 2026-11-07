# Creatives: advertisers' own creatives in the own book

The optional `creatives` module (`modules.creatives`, off by default; unreleased) lets the own book serve an
advertiser's own creative. An operator registers the creative. The module moderates it, conditions it to the stage's
ladder with the stage's own ingest pipeline, and checks that it conforms. Only then does the own book bid it. With
the `selling` module, a buy that a buyer agent books for a brand over AdCP is served with that brand's creative
([selling.md](selling.md#creatives)).

- [How a creative becomes READY](#how-a-creative-becomes-ready)
- [Register a creative](#register-a-creative)
- [What the own book does with it](#what-the-own-book-does-with-it)
- [Turn it on](#turn-it-on)
- [Cost](#cost)
- [Workarounds to remove](#workarounds-to-remove)
- [What it does not do](#what-it-does-not-do)

## How a creative becomes READY

Each creative is an item of the registry (`<prefix>-creatives`, keyed by its `crid`). One Step Functions state
machine, `<prefix>-creatives`, moves it from state to state, and every move is a conditional write with its history:

| State | Reached when |
|---|---|
| `RECEIVED` | It is registered. Its source is copied to the module's quarantine bucket, `incoming/<crid>/source.mp4` |
| `REVIEW` | Amazon Rekognition content moderation found a label at or above `min_confidence`. A person approves or rejects it |
| `APPROVED` | Moderation found no such label, or a person approved it (with `moderation: false`, at once) |
| `CONDITIONING` | The stage's ingest pipeline encodes it: a child execution of `<prefix>-ingest`, `kind: ad`, on the default ladder |
| `CONDITIONED` | The encode passed the ladder-conformance check |
| `READY` | It is published to the own book (`creatives/ready.json` in the PDS config bucket), and the snapshot publisher runs |
| `SUSPENDED` | An operator withdrew it from the own book; `resume` publishes it again |
| `REJECTED` | A person rejected it, or the check found it off the ladder or off the grid (the problems are in its history) |
| `FAILED` | A step failed: a source that is not a usable https MP4 or VAST document, moderation, or the encode. Register it again to retry |

The **source** is a local MP4, which the script uploads to the quarantine bucket, or an https URL of an MP4 or of a
VAST document. For a VAST document the module takes its best progressive MP4: the largest frame no taller than 1,080
lines, then the highest bitrate. It follows no Wrapper and reads no DOCTYPE. Sources never go to the ingest bucket,
whose upload rule ingests any video as content, and never to a bucket the CDN serves. They expire from quarantine after
30 days; the own book serves the encode.

The **conditioning** is the kit's own path ([ADR-0005](adr/0005-preconditioned-ads.md)): the same MediaConvert job
template as the content, so the creative matches the ladder by construction. The pipeline's Finalize writes the
creative's CDN copy under `ads/<ladder>/<creative id>/`.

The **ladder-conformance check** (`fast_channels.core.creatives.conformance`) states what MediaTailor does not check.
MediaTailor stitches an off-ladder streaming file by nearest match and never falls back to its progressive file. The
check requires:

- as many variants as the ladder's renditions, in its order, each with the rendition's codec family, resolution,
  frame rate and the muxed AAC audio;
- each variant's `BANDWIDTH` at most its rendition's nominal peak;
- one duration across the variants, a whole number of 30.03 s ad slots, to one frame.

An off-grid creative is rejected, not trimmed or padded: register a 30.03 s (or 60.06 s) cut.

## Register a creative

```bash
uv run python scripts/creatives.py register --prefix fastm --advertiser "Acme Outdoor" \
    --adomain acme-outdoor.example --file spot.mp4 --wait
uv run python scripts/creatives.py register --prefix fastm --advertiser "Acme Outdoor" \
    --adomain acme-outdoor.example --url https://ads.example/vast.xml --wait
uv run python scripts/creatives.py status --prefix fastm cr-0123456789ab
uv run python scripts/creatives.py approve --prefix fastm cr-0123456789ab --note "brand-safe"   # or reject
uv run python scripts/creatives.py suspend --prefix fastm cr-0123456789ab                      # or resume
```

`--adomain` is the brand's domain, the OpenRTB `adomain` of the creative's bids and the brand a booked buy names. The
same domain and source register the same creative (`cr-` and 12 hex digits of their hash). An agent can do the same
with `states:StartExecution` on `<prefix>-creatives`: the input is the
[`Registration`](../core/src/fast_channels/core/creatives/registry.py) or `Decision` JSON the script sends.

## What the own book does with it

- **Bids.** The snapshot publisher adds the READY creatives on the default ladder to the own book, marked
  `registered`, for the lines that serve them. A creative on another ladder is not bid: a ladder change freezes
  admission until the creative is registered again.
- **Ad buffet.** A first sight of a creative skips it in every session of a synchronized break (MediaTailor imports it
  when a session first asks, and the break's sessions all ask within about 3 s). So a registered creative counts as not
  yet WARM, and the pod rules place it only where a standalone backs its slot: MediaTailor's ad buffet plays the
  standalone if the creative is skipped. It is never a standalone itself. Without one it is left out (`unbacked` in the
  decision record) and the pod is selected again ([pds.md](pds.md#pod-rules)).
- **Prefetch.** On the release module's prefetched siblings the creative is imported at the schedule's retrieval,
  about two minutes before the break, so the break itself has no first sight.
- **Booked buys.** With `selling`, a buy for a brand gets the brand's first free READY creative, held by its line while
  the buy runs. A line on a demo creative moves to its brand's creative at the next sync. A brand with no free READY
  creative gets a demo creative, as without the module.

## Turn it on

`modules.creatives` needs `pds`:

| Key | Default | |
|---|---|---|
| `enabled` | `false` | The module |
| `moderation` | `true` | Moderate with Amazon Rekognition before conditioning |
| `min_confidence` | `80` | The label confidence (50–100) that sends a creative to review |

`make deploy` first: it lets the Media stack's ingest pipeline read the module's quarantine bucket (Prepare probes the
source with its own role, and MediaConvert reads it with its role). Then `make deploy-demand` deploys
`FastChannels-<prefix>-Creatives` after Pds, and `make destroy-demand` deletes it before Pds, emptying its bucket. The
shipped `fastm`, `fastci`, `fastciads` and `fastcisell` stages turn it on. `tests/e2e/test_24_creatives.py` proves it
on `fastcisell`: register, READY, book for the brand, air with no first-sight skip, delivered.

## Cost

Idle, nothing measurable: an on-demand table, a state machine, a function, and a bucket whose sources expire. Per
creative, Rekognition video moderation is $0.10 a minute (about $0.05 for a 30 s spot), and the encode is three
renditions of the spot at MediaConvert's basic rate (about $0.03).

## Workarounds to remove

Each is marked `OPTIMIZATION(aws): <id>` where it lives and listed with the repository's others in
[workarounds.md](workarounds.md).

| Id | AWS behaviour | Our workaround | Where | When it is fixed |
|---|---|---|---|---|
| `W-2 FR-3` | MediaTailor has no creative-readiness API (register, pre-transcode per variant set, query readiness) | The module itself: registry, moderation, conditioning by the ingest pipeline, conformance, READY | `infra/.../creatives/readiness.py`, `functions/.../creatives/readiness.py` | Register and pre-transcode through the API; keep the registry and conformance as the publisher's gate |
| `W-2 FR-2` | Concurrent first sights each start a transcode and skip the ad | A registered creative not yet WARM is placed only where a standalone backs its slot | `core/.../pds/pod.py` (rule 4), `core/.../pds/book.py` (`Book.cold`) | Drop the rule; standalones stay for late joiners |
| `W-10` item 6 | The AWS reference buyer has no creative tasks | An operator registers the brand's creatives (`scripts/creatives.py`) | `scripts/creatives.py`, `core/.../selling/booking.py` | Take the buyer's creatives from AdCP `sync_creatives` |

## What it does not do

- **OBSERVED and WARM.** The module does not yet learn that a creative aired cleanly (a `FILLED_AVAIL` without
  `NEW_CREATIVE`, then a quiet minute), so every registered creative keeps needing a standalone.
- **Trimming or padding** a source to the 30.03 s grid. It is rejected.
- **A buyer's streaming file.** The check is ready for one, but the module conditions every creative from its
  mezzanine.
- **Re-conditioning on a ladder change.** Register the creative again.
- **AdCP `sync_creatives`.** The seller would need the reference creative agent beside it (W-8 A9).
