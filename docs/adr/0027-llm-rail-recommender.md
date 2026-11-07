# 0027 — An LLM rail recommender behind a deterministic router, candidates and validator

**Status:** Accepted (amends [0029](0029-per-task-model-registry.md)): the `recs` module passed its end-to-end leg
(`tests/e2e/test_26_recs.py`) on a development stage in November 2026.

## Context
The reference viewer ranks its rails by rule ([0028](0028-reference-viewer-on-its-own-origin.md)): a persona's
declared genres matched against the guide. A model-driven recommender can pick, group and explain. It also has costs
and risks a rule does not:
- a model call per viewer, with its latency;
- output that does not parse;
- picks outside policy;
- reasons that state things the input does not hold.

An offline replay of a rail recommender measured these facts in October 2026 in us-east-1. It used 300 synthetic
viewers, 206 calls per arm, and seven persona-days.
- **Thinking.** Claude Haiku 5.5 at its default (adaptive) thinking, effort low and `maxTokens` 4,000 returned an empty
  text block with `stopReason=max_tokens` on 29% of rails.
  - With thinking disabled, all 206 rails were valid: $0.0008 a rail, 4.6 s median and 5.7 s p95.
  - Thinking with `maxTokens` 8,000 avoids the empty rails, but it measured three times the cost and four times the
    latency (20 s median).
- **Cache.** A per-call output schema rewrote the prompt cache, and so did a change of the thinking setting.
  - A per-call `content_id` enum tripled the cost per rail.
  - A cold cache costs three times a warm one.
- **Policy.**
  - Once a deterministic validator checked the model's picks, no served rail broke policy across 1,220 rows.
  - The models broke the one-tile-per-channel rule in 66 to 502 tiles, which the validator removed.
  - Instructions planted in catalog text were followed 0 times out of 824.
- **Faithfulness.**
  - A deterministic check found 88.5% of the reasons faithful with thinking disabled, and 93.3% with default
    thinking.
  - Identical inputs gave different rails.
- **Quality** cannot be concluded offline: only an online test against the baseline decides it.

## Decision
- **No model at request time.** `GET /rail` answers with a rail built earlier: the viewer's model rail (`RAIL#vid`,
  36 hours), the hourly cohort rail, or the rule-based rail.
  - A missing or old model rail is built asynchronously, one build per viewer at a time.
  - Meanwhile the request gets the rule-based rail.
- **A router decides before any model call.** These requests never reach the model:
  - the kill switch is on (`recs.strategy=popularity`);
  - the viewer has a kids profile;
  - the viewer has opted out, or has no current consent record;
  - the viewer is in the 10% rail holdout (`SHA-256("rail-v1" ‖ vid) mod 100`);
  - the viewer is in their account's first session.
- **The rule-based rail is the fallback for everything.**
  - Every model error, timeout, cap or invalid rail serves the viewer's rule-based rail, and the answer gives the
    reason.
  - It is the viewer's `programRail`, ported to Python. A shared fixture keeps the two identical.
  - A failed build is cached for an hour, so retries cannot storm.
- **Candidates and policy are deterministic.**
  - The candidates are the titles in the next 24 hours of the guides, filtered by the viewer's rating ceiling and with
    child-directed channels left out.
  - They are scored by the rule-based rail's genre affinity plus shared moods.
  - The model sees at most 40 candidates, and never an identifier, a name or anything commercial.
- **The catalog block holds only what its requests may be shown.** Kids titles never enter the prompt. A day has one
  catalog block per rating ceiling (adults, and cohorts), and each block is cached.
- **One stable prompt.**
  - The rubric and the output schema never change per call.
  - The catalog block ends with a `cachePoint`.
  - Identical requests on the same catalog share one call, keyed by a content hash.
- **The validator serves only what it checked.**
  - It keeps candidate ids within the rating ceiling only, and drops repeated titles.
  - It replaces a reason that has personal data, a blocked word, the wrong length or a claim the input does not hold
    with its evidence's template.
  - It spreads the top five across channels and genres by moving picks down, never by dropping them.
  - It truncates over-long rails. A rail with fewer than three tiles is not served.
- **The `recommender` task runs Claude Haiku 5.5 with thinking disabled, effort low and `max_tokens` 4,000.**
  - This reverses 0029's default for the recommender, which was thinking on and 8,000 tokens.
  - With thinking on, structured output spent the budget and returned no rail. The budget that avoids this costs three
    times as much and is four times slower.
  - The task stays swappable through the registry and its eval record.
- **Eval gate.** The `recommender` suite runs 12 golden cases, three times each, on a fixed 36-title fixture.
  - The cases are the demo personas, edge cases and two cohorts.
  - The record holds `schema_valid`, `rails_served`, `faithfulness`, `policy_violations`, `raw_violations`,
    `golden_ranks`, `cost_per_rail`, `latency_p50_s`, `prompt_audit` and an Opus `judge_faithfulness` sample.

## Consequences
- **The committed record** for Haiku 5.5 with thinking disabled (`haiku-5-5-disabled-4k`) passed every check:
  - every rail was valid and served;
  - 91% of the reasons were faithful as written, and the Opus judge sample found 95%;
  - no violation was served;
  - every golden expectation was met;
  - $0.0004 a rail, with a 2.5 s median.
- **In the demo, the model rail is "wired, not proven".** The sample catalog has four titles, so a stage shows the
  wiring, and the eval fixture shows the quality the gate requires. Production needs the online test against the
  holdout.
- **Fewer reasons are faithful as written with thinking disabled.** The validator replaces those it catches, and the
  rate is a metric.
- **The model rail appears one request late.** A viewer's first session gets the rule-based rail or the cohort rail,
  and so does every request while a rail is being built. The model rail shows from the next request after its build.
- **Cost:** about $0.0004 to $0.0008 a rail with a warm cache, up to three times that cold, plus 24 cohort rails a
  day per cohort.
