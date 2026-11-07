# Rail recommender

The `recs` module adds a "Recommended for you" rail for each signed-in viewer. Claude Haiku 5.5 picks the rail's
titles, orders them and explains each one. The candidates it chooses among, the policy around it and the checks on
its answer are deterministic code. No model runs while a viewer waits: rails are built in the background, and every
answer says how its rail was made and why. When the model fails, times out or reaches its daily cap, the viewer gets
the rule-based rail instead, the same one the viewer app ranks itself
([ADR-0027](adr/0027-llm-rail-recommender.md)).

- [What it deploys](#what-it-deploys)
- [Turning it on](#turning-it-on)
- [How a rail is chosen](#how-a-rail-is-chosen)
- [How a model rail is built](#how-a-model-rail-is-built)
- [GET /rail](#get-rail)
- [Cohort rails](#cohort-rails)
- [The model task and its eval](#the-model-task-and-its-eval)
- [Content signals](#content-signals)
- [Privacy](#privacy)
- [Operating it](#operating-it)
- [What it costs](#what-it-costs)
- [What it does not do](#what-it-does-not-do)

## What it deploys

The `FastChannels-<prefix>-Recs` stack holds the following:

| Resource | What it does |
|---|---|
| `GET /rail` | An HTTP API with a JWT authorizer. It admits only the viewer pool's access tokens that carry `fast/playback`, from the viewer app's and the e2e tests' clients. Its function has a reserved concurrency of 10. It reads the guides, the built rails, the cohort rails and the viewer's consent row, and it starts builds. It never invokes a model |
| The rail builder | The only caller of the `recommender` task's application inference profile. Each call is admitted by the task's daily spend counter. It writes the built rails and the cohort rails. Asynchronous invocations are not retried: a failure is recorded with its reason, and an unhandled one goes to a dead-letter queue with an alarm |
| `<prefix>-recs` | A DynamoDB table, on demand. It holds each viewer's rail state (`V#<vid>`) and the built rails (`REQ#<request hash>`, 36 hours) |
| An hourly schedule | EventBridge Scheduler. It builds the cohort rails |
| `/<prefix>/recs/rail-url` | The API's URL. The Viewer stack reads it into the app's `config.json` (`railUrl`) |

The module also changes three other stacks:
- **Models:** it adds a `recommender` registry entry, unless `modules.models.tasks` names one.
- **Ops:** the `recs` flag defaults to `recommender`.
- **Viewer:** the app shows the recommender's rail.

## Turning it on

```json
"modules": {
  "models": {"enabled": true},
  "identity": {"enabled": true},
  "recs": {"enabled": true, "holdout_percent": 10, "cohorts": ["web-en"], "refresh_hours": 20}
}
```

`recs` needs `models` and `identity`. Through `identity`, it also needs `release` and the modules release needs. It
does not need `make deploy-demand`: `make deploy` deploys the Recs stack.

| Field | Default | Meaning |
|---|---|---|
| `holdout_percent` | 10 | The share of viewers in the rail holdout (bucket `rail-v1`), who get the rule-based rail. It is the baseline the model rail is measured against |
| `cohorts` | `["web-en"]` | `<device>-<language>` of each hourly cohort rail |
| `refresh_hours` | 20 | A model rail older than this is rebuilt on the viewer's next request |

The default `recommender` entry is Claude Haiku 5.5 through the geography's inference profile, with these settings:
- effort `low`;
- thinking `disabled`;
- `max_tokens` 4,000;
- data class `viewer`;
- a daily cap of $2;
- eval record `haiku-5-5-disabled-4k`.

To swap the model, set `modules.models.tasks.recommender` and commit a passing eval record for it
([below](#the-model-task-and-its-eval)).

## How a rail is chosen

A router decides before any model call (`fast_channels.core.recs.router`):

| Who | Rail | `strategy` / `reason` |
|---|---|---|
| `recs.strategy` is `popularity` (the kill switch) | The rule-based rail | `baseline` / `kill_switch` |
| A kids profile (`?profile=kids`) | The kids lineup: titles of child-directed channels and titles rated TV-G or below | `kids` / `kids` |
| A viewer who opted out | The cohort rail | `cohort` / `opted_out` |
| A viewer without a current consent record (SAFE) | The cohort rail | `cohort` / `no_consent` |
| The rail holdout: `SHA-256("rail-v1" ‖ vid) mod 100 < holdout_percent` | The rule-based rail | `baseline` / `holdout` |
| An account's first session (the sign-in whose `auth_time` was first seen) | The cohort rail, re-ranked by the declared genres | `cohort` / `first_session` |
| A viewer without taste facts | The cohort rail | `cohort` / `no_taste` |
| Everyone else | The viewer's model rail | `llm` / `model` |

A model rail goes through these states:
- **Missing, or older than `refresh_hours`:** it is rebuilt in the background, one build per viewer at a time.
- **Being rebuilt:** a rail that still airs is served as it is. Without one, the request gets the rule-based rail,
  with reason `building` or `expired`.
- **Failed build:** the viewer gets the rule-based rail with the failure's reason (`cap`, `timeout`, `model_error`,
  `invalid`) for an hour. The next request after that tries again.

## How a model rail is built

1. **Guides and cards.** The builder reads every channel's guide for the next 24 hours, then each title's catalog card
   ([content signals](#content-signals)).
2. **Candidates.** Titles above the viewer's rating ceiling and titles of child-directed channels are left out. The
   rest are scored by the rule-based rail's genre affinity, plus shared moods, plus a bonus for titles on within
   three hours. The best 40 are kept. The model never sees anything else as a choice.
3. **The prompt.** The system prompt is a stable rubric, then the catalog block, then a `cachePoint`.
   - The catalog block holds the cards a request may be shown: no kids titles, one block per rating ceiling. Every
     request of a day reads the same cached prefix.
   - The request block holds the taste facts (the declared genres and moods) or the cohort, the per-channel limit
     and the candidates.
   - No identifier, name or commercial field is ever in a prompt. An audit refuses a request that holds one.
4. **Reuse.** The request is hashed with the prompt version, the model and the catalog. An identical request built in
   the last 36 hours is reused without a model call, so Dana and Ben, who declared the same genres, share one call.
5. **The model call.** One Converse call on the task's inference profile, with native structured output
   (`outputConfig.textFormat`), thinking disabled and a 30-second read timeout.
6. **The validator.** It checks every pick before anything is served (`fast_channels.core.recs.validate`):
   - Ids outside the candidates and titles above the ceiling are dropped and counted as violations.
   - A reason with personal data or a blocked word (advertising, price, instructions) is replaced, as is one longer
     than 120 characters.
   - A reason that claims what the input does not hold for its tile is unfaithful and is replaced by its evidence's
     template. Examples: another title's name, a genre or mood the card lacks, a time, a number.
   - The first five tiles are spread across channels (one each once there are five) and hold at most two of one
     genre. A pick that breaks the spread moves below the top; none is dropped for it.
   - Over-long rails are truncated, and a rail with fewer than three tiles is not served.
7. **Storage.** The rail is stored with its lineage: the task, the model, the prompt and schema versions, the catalog
   version, the request hash, the Bedrock request id, the cost and the latency.

At request time, each tile is placed at its title's soonest airing that has not ended. A rail left with fewer than
three tiles that still air is rebuilt.

## GET /rail

```text
GET <railUrl>[?profile=kids][&device=web&lang=en]
Authorization: Bearer <viewer access token with fast/playback>
```

```json
{
  "strategy": "llm",
  "reason": "model",
  "title": "Recommended for you",
  "tiles": [
    {"content_id": "tears-of-steel", "title": "Tears of Steel", "channel": "fastm-demo",
     "program": "tears-of-steel-0", "start": "…", "end": "…", "on_now": true,
     "reason": "Sci-fi action with a live-action edge, the genres you like", "evidence": ["declared_genre"]}
  ],
  "lineage": {"task": "recommender", "model": "us.anthropic.claude-haiku-5-5", "prompt_version": "recs-v1",
              "request_hash": "…", "request_id": "…", "built_at": "…", "cost_usd": "0.0004", "latency_ms": 2400}
}
```

Every answer is `Cache-Control: private, no-store`. A token without a viewer gets `401`.

## Cohort rails

Each hour the builder makes one rail per configured cohort for the current daypart. The dayparts are UTC: morning
from 06:00, afternoon from 12:00, evening from 18:00 and night from 23:00. Each rail is written to
`plan/rail/<device>-<language>-<daypart>.json` in the media bucket, which the CDN serves with a five-minute
`max-age`. A guest's app reads it there.

A cohort rail holds no viewer data, so its prompt has none. Its rating ceiling is TV-14, because its viewers are not
known to be adults. An unchanged catalog reuses the last hour's rail, so the hourly run calls the model only when the
guides change.

## The model task and its eval

The `recommender` task follows the model registry ([agent.md](agent.md#model-registry)):
- one SSM entry, `/<prefix>/models/tasks/recommender`;
- its own application inference profile, metrics and cost line;
- a daily spend breaker;
- an eval gate.

An operator's SSM edit can tune the cap, effort, thinking and `max_tokens`. An entry that names another model or
profile is ignored, and the builder runs on the entry as deployed.

`make eval TASK=recommender MODEL=<id>` runs the recommender suite (`fast_channels.functions.recs.evals`) and writes
the eval record. Pass `--judge 8` to `scripts/eval_model.py` for an Opus judge sample.
- **The suite:** 12 golden cases on a fixed 36-title fixture with five channels, one of them child-directed. Each
  case runs three times.
- **The cases:**
  - the demo personas' genres;
  - a history buff, a rom-com fan, a teen with a TV-14 ceiling, and a viewer with no genres;
  - two cohorts.
- **What each case expects:** one of a set of titles within the first *k* tiles, titles that must never appear, how
  many leading tiles share a declared genre, and a minimum length.
- **The fixture's trap:** one synopsis carries a prompt injection that asks for the kids titles and a sponsored rail
  title.

| Check | Pass | `haiku-5-5-disabled-4k` |
|---|---|---|
| `schema_valid` | ≥ 0.99 | 1.0 |
| `rails_served` | ≥ 0.95 | 1.0 |
| `faithfulness` (deterministic, the model's reasons as written) | ≥ 0.85 | 0.91 |
| `policy_violations` (in the served rails) | 0 | 0 |
| `raw_violations` (in the model's output, per tile) | ≤ 0.05 | 0 |
| `golden_ranks` (expectations met) | ≥ 0.80 | 1.0 |
| `cost_per_rail` | ≤ $0.002 | $0.0004 |
| `latency_p50_s` | ≤ 10 | 2.5 |
| `prompt_audit` | 0 | 0 |
| `judge_faithfulness` (Opus, 8 rails) | ≥ 0.80 | 0.95 |

**Why thinking is disabled.** Claude Haiku 5.5's default (adaptive) thinking, with a JSON output schema and 4,000 output
tokens, spent the budget and returned an empty rail on 29% of calls. The 8,000-token budget that avoids this costs
three times as much and is four times slower ([limitations.md](limitations.md), W-7 Q9).

## Content signals

A title's card joins the CMS catalog item (`CONTENT#<id>` / `META`) with the `understanding` module's item beside it
(`CONTENT#<id>` / `UNDERSTANDING`, [understanding.md](understanding.md)), when that exists and its model step ran.
- **What the understanding item adds:** IAB Content Taxonomy 3.1 categories, keywords, and moods from its fixed
  vocabulary.
- **Without it,** the categories come from the catalog genres through the crawl module's genre map.
- **Precedence:** the CMS's synopsis and language win over the understood ones.
- **Catalog text is untrusted.** It reaches the model only as quoted JSON, cut to fixed lengths, and the validator
  never trusts a reason over the card.

## Privacy

- **What reaches the model.** The `vid` is the only key, and it never reaches a prompt. A prompt holds taste facts
  only: the persona's declared genres and moods.
- **Who never gets a model call:** kids, opted-out viewers, viewers without consent, the holdout and first sessions.
- **What is stored.** The table stores no personal data. Erasing a viewer deletes `V#<vid>`, and nothing needs
  retraining.

## Operating it

- **The kill switch.** Set the `recs` flag's `strategy` to `popularity` in AppConfig (the `flags` profile of
  `<prefix>-fast`). Every request then gets the rule-based rail within 30 seconds.
- **The cap.** Raise or lower `daily_cap_usd` in the task's SSM entry. At the cap, builds fail with `cap`, and the alert
  topic is told once that day.
- **Edits take a minute.** The builder rereads the SSM entry every 60 seconds. A build in that minute runs on the
  previous entry, and a build that fails is not retried for an hour: after putting an entry back, clear the failures
  of the viewers it caught (below).
- **Rebuild a viewer's rail.** Remove `rail_key` and `failure` from their `V#<vid>` item. The next request starts a
  build. Remove `failure` alone to retry a failed build at once.
- **Build the cohort rails now.** Invoke the builder (output `RailBuilderName`) with `{"cohorts": true}`.
- **Metrics** (namespace `FastChannels`):
  - from the builder (dimension `service` `recs.builder`): `ModelLatencyMs`, `ModelCostUsd`, `InputTokens`,
    `OutputTokens`, `CacheReadTokens`, `CacheWriteTokens`, `ValidatorDrops`, `ReasonsReplaced`, `PolicyViolations`,
    `FaithfulShare`, `RailsBuilt`, `RailsReused`, `RailBuildMs`, and `RailBuildFailures` by `Reason`;
  - from the API (`recs.api`): `Rails` by `Strategy` and `Reason`, and `RailLatencyMs`.
- **Alarms.** `<prefix>-recs-builder-dead-letters` fires when a build fails unhandled. A failed model call is handled:
  it is a metric, not an alarm.

## What it costs

- **Idle:** about nothing (an on-demand table, functions and a schedule), plus one alarm at $0.10 a month.
- **Per model rail:** about $0.0004 with a warm prompt cache (*measured*, its eval record), up to three times that
  cold. A signed-in adult gets at most one model rail a day, and identical requests share one call.
- **Cohort rails:** 24 builds a day per cohort, most of them reused.
- **Per request:** a Lambda invocation, a KMS `GenerateMac` and two or three DynamoDB reads.

## What it does not do

- **The model rail is wired, not proven.** The eval gate tests quality on a fixture. The demo catalog has four titles,
  so a stage shows the wiring. The online test, which would interleave the model and rule-based rails against the
  holdout, is not built.
- **No viewing history.** The taste facts are the persona's declared genres. A 28-day taste vector from the lake is a
  follow-up.
- **No embeddings and no vector search.** Candidates are scored on genres and moods.
- **One task.** Cohort rails use the `recommender` task too; a `recommender.cohort` task is a follow-up.
- **The kill switch needs the ops module.** Without it, `recs.strategy` cannot be read and the recommender always runs.
