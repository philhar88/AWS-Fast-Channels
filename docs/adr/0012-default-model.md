# 0012 — Claude Haiku 5.5 as the default model, pluggable via a model spec

**Status:** Accepted

## Context
The agent must be cheap by default and replaceable. A scheduling benchmark (fill 3 h and 6 h blocks under programming
rules, ad cadence and ratings) gave: Claude Haiku 5.5 12/12 passes at about $0.003–0.006 per schedule; Claude Sonnet 5.5
12/12 at about 12× the cost; Amazon Nova 2 Lite, Micro and Pro 0/27; Claude Haiku 4.5 failed.

## Decision
Default model: the region's geographic inference profile of Claude Haiku 5.5 (`us.anthropic.claude-haiku-5-5` in US
regions). The runtime reads a validated model spec (model id from an IAM allow-list, effort per mode, max tokens) from SSM
Parameter Store with a code default; `global.` profiles are opt-in. Tools do all arithmetic, so smaller models only
choose. `make eval MODEL=…` runs the conformance suite a model must pass before you switch.

## Consequences
New accounts must enable Anthropic model access first (`make model-access`). Claude 5.x rejects `temperature`, so the
agent never sends it.
