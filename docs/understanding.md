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
- [Turning it on](#turning-it-on)
- [Cost](#cost)
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
   25 labels with the most screen time, and the top-level moderation categories seen at least twice at 80 %
   confidence or more.
5. **Describe.** One Bedrock Converse call through the registry task `understanding.describe`, under the task's
   daily spend cap. The model sees the proxy (by its S3 URI), the signals and the excerpt. Video models read pictures
   only (Amazon Nova has no audio), which is why the transcript travels as text. The model answers through one forced
   tool call, and its answer is checked, never trusted:
   - category ids outside Content Taxonomy 3.1 are dropped (at most 5 are kept);
   - keywords are trimmed and lowered (at most 10), moods outside the vocabulary are dropped (at most 3);
   - the synopsis is cut at a word (at most 300 characters);
   - the brand-safety tier is the riskier of the model's and the moderation categories' tier.

   The step writes the item and puts `Catalog Item Changed` and `Content Understood` on the deployment's bus.

The system prompt (the instructions, the vocabularies and the 704 Content Taxonomy 3.1 categories) is the same for
every title and carries a cache point.

## The item

`ReferenceCatalog` item `pk = CONTENT#<content_id>`, `sk = UNDERSTANDING`, beside the CMS's `META` item, which the
module never writes: a catalog sync never erases an analysis, and an analysis never edits the CMS's data. The schema
is [`contracts/content-understanding.schema.json`](../contracts/content-understanding.schema.json), version 1.

| Field | What it holds |
|---|---|
| `v` | `1`; a new schema is a new version, never an in-place change |
| `content_id`, `asset_id` | The title, and the encode that was analysed |
| `status`, `analyses` | `complete`, or `partial` when an analysis or the model step failed; the analyses that succeeded |
| `cat` | Content Taxonomy 3.1 category ids (AdCOM `cattax` 9), most apt first, at most 5 |
| `keywords` | Lower-case descriptive keywords, at most 10 |
| `moods` | At most 3 of: uplifting, tense, dark, humorous, romantic, calm, energetic, sad, suspenseful, whimsical, epic, informative (`fast_channels.core.understanding.MOODS`) |
| `brand_safety`, `moderation` | `low`, `medium`, `high` or `floor`, and the top-level moderation categories behind it |
| `synopsis` | One or two sentences, without spoilers or real people's names, at most 300 characters |
| `language`, `speech_ratio`, `shots`, `duration_ms` | From the analyses |
| `model`, `analyzed_at` | The foundation model the describe step ran on (null when it did not run), and when |

Categories, keywords, moods and the synopsis come from the model step only: an item with a null `model` has none.
The brand-safety tiers of the moderation categories are in `fast_channels.core.understanding.signals`
(`MODERATION_TIERS`): explicit content and hate symbols are `floor`, violence and disturbing imagery `high`, drugs,
tobacco, suggestive content, gambling and rude gestures `medium`, alcohol and swimwear `low`.

The recommender reads the item with the title's `META` item, and the event `Content Understood` (`content_id`,
`asset_id`, `status`, `correlation_id`) tells other consumers.

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
- Synth refuses the entry without a passing eval record of the task, as for every registry task.
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
| Amazon Nova 2 Lite: at most 960 frames (about 276,000 tokens) and about 25,000 text tokens in, 400 out | $0.33 and $2.75 per million tokens | about $0.10 |
| Step Functions, Lambda, S3 | | under $0.01 |

Rekognition is 95 % of the cost. Cheaper sets:

- `["transcript", "shots", "moderation"]`: about $9.90 per content hour. The model still sees every frame it
  samples, so labels and credits are the only signals lost.
- `["transcript", "moderation"]`: about $6.90. No shot count.
- `["transcript"]`: about $0.90. The brand-safety tier is then the model's alone.

The stack itself costs nothing while no title is analysed but its failure alarm ($0.10 a month).

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
