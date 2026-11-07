# Content understanding

The `understanding` module (`modules.understanding`, M12) analyses each title once, on its first encode, and records
what the title is about: IAB Tech Lab Content Taxonomy 3.1 categories, keywords, moods, a brand-safety tier and a
synopsis, beside the title's catalog item. It reads the title's own pictures and speech, not the catalog, so it
also covers titles whose CMS metadata is thin. It is off by default.

AWS Elemental Inference, AWS's own video understanding for media, analyses live video only: it has no API for a file
or a VOD asset. Until it has, this module glues general-purpose services together: AWS Elemental MediaConvert,
Amazon Transcribe, Amazon Rekognition Video and a Bedrock video model through the [model registry](agent.md#model-registry).
It is marked `OPTIMIZATION(aws): EI-VOD` ([workarounds.md](workarounds.md#other-aws-services),
[limitations.md](limitations.md#other-aws-services)), and the item's contract stays when Elemental Inference takes
over.

- [How it works](#how-it-works)
- [The item](#the-item)
- [Brand safety](#brand-safety)
- [Turning it on](#turning-it-on)
- [Cost](#cost)
- [The eval suite](#the-eval-suite)
- [Analysing a title again](#analysing-a-title-again)
- [Failures](#failures)
- [Limits](#limits)
- [Privacy](#privacy)

## How it works

The stack `FastChannels-<prefix>-Understanding` holds one Step Functions workflow, `<prefix>-understanding`. An
EventBridge rule starts it on each `Asset Ready` event of a content asset.

1. **Prepare.** The step reads the asset's catalog `content_id` from the media library. It skips an asset without
   one, and a title that already has an item: a title is analysed once, whatever its later encodes.
2. **Proxy.** MediaConvert (Basic tier) encodes a 640 by 360 MP4 from the encode's smallest HLS variant that is at
   least 360 lines tall. MediaConvert reads HLS with MPEG-TS segments as an input; Rekognition and Transcribe do not,
   and a video model reads at most 1 GB from S3. The proxy is kept for a day under `proxies/` in the module's
   bucket.
3. **Analyses**, in parallel, each started by the workflow and polled every 30 seconds:
   - `transcript`: a Transcribe batch job with language identification;
   - `shots` and `technical_cues`: one Rekognition segment detection job (shots; credits, black frames, color bars,
     slates, studio logos);
   - `labels`: Rekognition label detection (objects, scenes, activities), aggregated by segment;
   - `moderation`: Rekognition content moderation, the visual evidence of the brand-safety tier.
4. **Collect.** The step pages each finished analysis's results, keeps them raw for 30 days under `results/`, and
   reduces them to signals: the spoken language, the share of the runtime with speech, a transcript excerpt (at
   most 8,000 characters: its start, middle and end), the shot count and mean shot length, the technical cues, the
   25 labels with the most screen time, and every moderation label seen in at least two frames at 80 %
   confidence or more, with its frames ([Brand safety](#brand-safety)).
5. **Describe.** One Bedrock Converse call through the registry task `understanding.describe`, under the task's
   daily spend cap. The model sees the proxy (by its S3 URI), the signals and the excerpt. Video models read pictures
   only (Amazon Nova has no audio), which is why the transcript travels as text. The model answers through one forced
   tool call, and its answer is checked, never trusted:
   - the model names one to three genres from the IAB CTV genres, and each becomes its Content Taxonomy 3.1 id
     through the genre map that maps the catalog's own genre tags
     ([content-enrichment.md](content-enrichment.md#genres-and-categories)); other values are dropped;
   - keywords are trimmed and lowered (at most 10), moods outside the vocabulary are dropped (at most 3);
   - the synopsis is cut at a word (at most 300 characters);
   - the brand-safety tier is the riskier of the model's and the moderation labels' tier.

   The step writes the item and puts `Catalog Item Changed` and `Content Understood` on the deployment's bus.

The categories are the title's genres because that is what Amazon Nova 2 Lite names reliably. Given the 704 Content
Taxonomy 3.1 categories and asked for ids, it copied the list's order: Animation & Anime, Comedy, Drama and Factual
for every sample title, including the live-action Tears of Steel, and Attractions as a subject. What a title is
about goes into its keywords, which bidders receive in `kwarray`.

## The item

`ReferenceCatalog` item `pk = CONTENT#<content_id>`, `sk = UNDERSTANDING`, beside the CMS's `META` item, which the
module never writes: a catalog sync never erases an analysis, and an analysis never edits the CMS's data. The schema
is [`contracts/content-understanding.schema.json`](../contracts/content-understanding.schema.json), version 1.

| Field | What it holds |
|---|---|
| `v` | `1`; a new schema is a new version, never an in-place change |
| `content_id`, `asset_id` | The title, and the encode that was analysed |
| `status`, `analyses` | `complete`, or `partial` when an analysis or the model step failed; the analyses that succeeded |
| `cat` | The Content Taxonomy 3.1 ids (AdCOM `cattax` 9) of the title's genres, most apt first: at most 3 (the schema allows 5) |
| `keywords` | Lower-case descriptive keywords, at most 10 |
| `moods` | At most 3 of: uplifting, tense, dark, humorous, romantic, calm, energetic, sad, suspenseful, whimsical, epic, informative (`fast_channels.core.understanding.MOODS`) |
| `brand_safety`, `moderation` | A GARM-style risk tier, `low`, `medium`, `high` or `floor` ([Brand safety](#brand-safety)), and the top-level moderation categories seen |
| `synopsis` | One or two sentences, without spoilers or real people's names, at most 300 characters |
| `language`, `speech_ratio`, `shots`, `duration_ms` | From the analyses |
| `model`, `analyzed_at` | The foundation model the describe step ran on (null when it did not run), and when |

Categories, keywords, moods and the synopsis come from the model step only: an item with a null `model` has none.

Who reads the item:

- **The catalog context** ([content-enrichment.md](content-enrichment.md#genres-and-categories)). With the crawl
  module, the publisher reads it with the `META` item and writes the title's context object again when the item
  changes: the item's categories lead `cat`, its keywords join `kwarray`, and its language fills an empty `language`.
  The Pod Decision Service patches `app.content` from that object, so bidders see what the title is about even when
  the CMS sends thin metadata.
- **The recommender** reads it with the title's `META` item.
- Other consumers can follow the event `Content Understood` (`content_id`, `asset_id`, `status`, `correlation_id`).

## Brand safety

`brand_safety` is a risk tier in the style of the GARM brand safety floor and suitability framework, from the least
to the most risk: `low`, `medium`, `high` and `floor`, which is content no advertiser should appear next to.
`high` means high risk, not highly safe.

**Nothing gates demand on it yet.** The catalog context, and so `app.content`, carries the item's categories,
keywords and language, not its tier. The Pod Decision Service, Prebid Server and the recommender don't read it, and
`Content Understood` doesn't carry it. A consumer that restricts bidders or brands by the tier is still to be
built.

The tier is the riskier of two:

- **The model's,** from the tier descriptions in its prompt, which name each tier's moderation labels.
- **The moderation labels',** by the most specific label Rekognition reports, in
  `fast_channels.core.understanding.signals.MODERATION_TIERS`:

  | Tier | Labels |
  |---|---|
  | `floor` | Explicit nudity and sexual activity, sex toys, hate symbols (Nazi Party, White Supremacy, Extremist) |
  | `high` | Blood & Gore, Self-Harm, Death and Emaciation (Emaciated Bodies, Corpses) |
  | `medium` | Weapons, weapon and physical violence, explosions, crashes, drugs and tobacco, non-explicit nudity, suggestive content, rude gestures, gambling |
  | `low` | Alcohol, swimwear or underwear, kissing |

  A label that is not listed takes its top-level category's tier, and an unknown category counts as `medium`.
- **The exposure rule:** a `high` or `floor` label counts at its tier only when it was seen in at least 10 frames at
  90 % confidence or more. Rekognition samples about two frames a second, so that is about five seconds on screen.
  Below that, the label gives one tier less. Any label needs two frames at 80 % to count at all.

On the sample titles, Sintel, Tears of Steel and Elephants Dream are `medium` and the Sintel trailer is `low`:
- Sintel has two frames of blood;
- Tears of Steel has sustained weapon violence;
- Elephants Dream has three frames that read as emaciated bodies.

Before this calibration, which rolled the labels up to their top-level category and had no exposure rule, all
three were `high`. Unit tests cover the `high` and `floor` cases with synthetic labels, because no open-licensed
title has them.

## Turning it on

The module needs the model registry (`modules.models`) and a registry entry for its task, a video model the
deployment allows:

```json
"agent": {"allowed_models": ["{geo}.anthropic.claude-haiku-5-5", "...", "{geo}.amazon.nova-2-lite-v1:0"]},
"modules": {
  "models": {"enabled": true, "tasks": {
    "understanding.describe": {"model": "{geo}.amazon.nova-2-lite-v1:0", "effort": "low", "thinking": "disabled",
                               "max_tokens": 4000, "daily_cap_usd": 5, "eval": "<a passing eval record>"}}},
  "understanding": {"enabled": true}
}
```

- Adding the model to `agent.allowed_models` grants nothing to the scheduler agent: with the registry on, its
  runtime may invoke only its own task's profile. The describe step may invoke only the `understanding.describe`
  profile, the model only through it.
- Synth refuses the entry without a passing eval record of the task, as for every registry task. The record of
  the default entry is committed ([The eval suite](#the-eval-suite)).
- `core.understanding.REGISTRY_DEFAULT` is that entry. The shipped stages (`fastm`, `fastci`, `fastciads`) turn the
  module on with it.
- `modules.understanding.analyses` chooses the analyses ([Cost](#cost)); `modules.understanding.task` names another
  registry task.

## Cost

A title is analysed once, so the cost is one-time per content hour, not a running one. With every analysis on (the
default), it is about **$18.90 per content hour** at us-east-1 list prices (AWS Price List API, 2026-11-07):

| Step | Unit price | Per content hour |
|---|---|---|
| Rekognition label detection (`labels`) | $0.10 a minute | $6.00 |
| Rekognition content moderation (`moderation`) | $0.10 a minute | $6.00 |
| Rekognition segment detection, shots (`shots`) | $0.05 a minute | $3.00 |
| Rekognition segment detection, technical cues (`technical_cues`) | $0.05 a minute | $3.00 |
| MediaConvert proxy, Basic tier, SD | $0.0075 a minute | $0.45 |
| Transcribe batch (`transcript`) | $0.0001 a second | $0.36 |
| Amazon Nova 2 Lite: at most 960 frames of 256 tokens and at most 4,500 text tokens in, about 400 out | $0.33 and $2.75 per million tokens | about $0.08 |
| Step Functions, Lambda, S3 | | under $0.01 |

Measured on the four sample titles (38.8 minutes, 2026-11-07): $12.37 in all, **$19.12 per content hour**. The
billed units were each title's proxy minutes for Rekognition and MediaConvert (one output, Basic tier, SD), its
seconds for Transcribe, and the tokens the describe step logged (229,477 for Sintel's 888 frames). The model's share
was $0.20: titles under 16 minutes are read at one frame a second, $0.31 per content hour, while an hour-long title
is read as 960 frames, about $0.08. A title took 9 to 12 minutes from `Asset Ready` to its item, whatever its
length.

Rekognition is 95 % of the cost. Cheaper sets:

- `["transcript", "shots", "moderation"]`: about $9.90 per content hour. The model still sees every frame it
  samples, so labels and credits are the only signals lost.
- `["transcript", "moderation"]`: about $6.90. No shot count.
- `["transcript"]`: about $0.90. The brand-safety tier is then the model's alone.

The stack itself costs nothing while no title is analysed but its failure alarm ($0.10 a month).

## The eval suite

`make eval TASK=understanding.describe MODEL=us.amazon.nova-2-lite-v1:0` qualifies a model for the describe task
(`fast_channels.functions.understanding.evals`). Its cases are the four sample titles: the signals a live run's
analyses gave each one (`fixtures/<content_id>.json`) and an excerpt of its pictures, which the suite cuts from the
title's public source with ffmpeg (up to three one-minute windows at 640 by 360, among them the scenes that the
moderation labels flagged). Every case runs three times through the real prompt, the forced tool call and the
checks, with the excerpt sent inline; only Bedrock is called. The suite writes
`evals/records/understanding.describe/<key>.json`, passing or not.

| Check | Pass |
|---|---|
| `schema_valid` | at least 99 % of the calls call the tool with every field |
| `categories_valid` | at least 90 % of the genres the model names are in the vocabulary |
| `expected_categories` | at least 80 % of the calls have one of the title's expected genres among the first three categories, which the catalog context carries to bidders (such as Animation & Anime, Fantasy or Science Fiction) |
| `no_wrong_categories` | at least 90 % of the calls name none of the title's clearly wrong genres (Documentary, Factual or Reality TV for any of the films, Animation & Anime for Tears of Steel) |
| `moderation_agreement` | at least 90 % of the calls give a tier at most one step below the moderation labels' |
| `brand_safety_expected` | at least 80 % of the calls give the item the title's expected tier, the riskier of the model's and the labels' (`medium` for the three films, `low` for the trailer) |
| `moods_valid` | at least 90 % of the moods are in the vocabulary |
| `cost_per_hour` | the model's cost of an hour-long title, at most $0.50 |

The committed record is Amazon Nova 2 Lite's with the default entry: every check passed, at $0.09 for an hour-long
title and $0.16 for the run, and every call gave the expected tier. The prompt names each tier's moderation labels:
without them, the model rated Sintel `low` in two calls of three, against moderation labels of graphic violence.

The request carries no cache point. With one on the system prompt, Nova 2 Lite reported most of each title's video
tokens as cache reads (the first title's as cache writes), though no video was ever sent twice; its Price List
prices those at a quarter of the input price and at $0. The suite counts every input token at the input price, so
its cost check holds either way.

On a stage, `tests/e2e/test_27_understanding.py` analyses the end-to-end test pattern and, with the crawl and demand
modules, follows its categories and keywords into the catalog context object and a bidder's `app.content`.

## Analysing a title again

Start the workflow by hand with the title's `Asset Ready` detail and `"force": true`:

```bash
aws stepfunctions start-execution --state-machine-arn <UnderstandingStateMachineArn> \
  --input '{"force": true, "detail": {"asset_id": "...", "kind": "content", "ladder_version": "...", "vod_source": "...", "duration_ms": 888000, "correlation_id": "manual"}}'
```

## Failures

The workflow fails open:

- an analysis that fails is left out of the signals, and the item is `partial`;
- a model call that fails, is refused or meets the task's daily cap leaves the description out (no categories,
  keywords, moods or synopsis), and the item is `partial`, its brand-safety tier from the moderation labels alone;
- only a failed `Prepare` or proxy fails the execution, which the alarm `<prefix>-understanding-failed` reports.

The describe step publishes `TitlesUnderstood`, `TitlesUnderstoodPartially` and `UnderstandingCostUsd` (a list-price
estimate) in the `FastChannels` namespace.

## Limits

- Amazon Nova 2 Lite samples one frame a second up to 16 minutes and 960 frames over a longer title, which is
  enough for title-level tags.
- Nova refuses frames that break the acceptable use policy; such a title's item is `partial`.
- Rekognition Video runs 20 concurrent jobs per API by default; a bulk ingest queues behind it.
- Transcribe's language identification covers its supported languages; a title without speech has no language.

## Privacy

The analyses read the title's own media only, never a viewer's data. The proxies expire after a day and the raw
results (the transcript among them) after 30 days. The item holds the title's description, not its transcript.
