# 0032 — One as-run subscription per account, routed through EventBridge

**Status:** Accepted

## Context

Channel Assembly writes every channel's as-run events to one log group per account and region,
`MediaTailor/Channel/AsRunLog`. Each FAST Channels deployment subscribes to it with a filter for its own channels
(`<prefix>-as-run`), so that the reconciler can mark programs, and with the ops module breaks, as aired.

CloudWatch Logs allows five subscription filters per log group (quota `L-87E7D306`, not adjustable). So v3.0 supports
at most five deployments per account and region: a sixth one fails to create its filter, and its whole Media stack
rolls back. That is enough for production, staging and development, but not for parallel development and test stages:
in October 2026 a live stage, a shared development stage and three test stages held all five filters, so no further
deployment could be created in the account and region.

## Decision

- **Opt-in.** `observability.as_run` chooses how a deployment reads the as-run log:
  - `per_stage` (the default): its own subscription filter, exactly as in v3.0;
  - `shared`: an EventBridge rule on the account's default bus for the events of its own channels (`detail.channelName`
    with the prefix `<prefix>-`), which invokes the same reconciler.
- **The router.** The router is the account-level stack `FastChannels-AsRun`. It holds the one subscription filter,
  `fast-channels-as-run-router`, on all events. A function (`fast_channels.functions.ops.as_run_router`) puts each
  as-run event on the default bus with:
  - source `fast-channels.as-run`;
  - the event type as detail-type;
  - the event plus `as_run_id` (log stream and log event id) as detail.

  Entries are put ten at a time, and refused entries are put again.
- **Correlation.** `as_run_id` is the correlation id from end to end: the router logs the ids it puts (its Powertools
  correlation id is the batch's first), and the reconciler logs the id of each event it handles, so
  `scripts/trace.py <as_run_id>` finds the reconciler's lines.
- **Deployment.** `make as-run-router STAGE=<stage>` deploys the stack (`-c as-run-router=true` adds it to the
  synthesized stage, with that stage's log retention and alert email). Without that flag no command synthesizes it,
  so `make deploy` never changes it and `make destroy` never tries to delete it. Were it part of the stage,
  `make destroy` would stop at the protected stack before deleting the stage's own stacks.
- **Failure handling.** CloudWatch Logs invokes the router asynchronously: two retries, then a dead-letter queue.
  Alarms on its errors, its throttles and the queue notify `fast-channels-as-run-alerts`. The rule's target retries the
  reconciler eight times. Delivery is at least once: a retried batch is put again, and the reconciler's conditional
  updates make a repeated event a no-op.
- **Shared stages.** The shared development stage (`fastm`) and the test stages (`fastci`) use `shared`. No stage's
  teardown touches the router.

## Consequences

- Any number of `shared` deployments can coexist. Only the router and the `per_stage` deployments take a filter.
- **Lifecycle risk.** The router is an account-level singleton like `FastChannels-AccountBaseline`, and its stack is
  termination protected.
  - Every `shared` deployment depends on it, but none of them deploys or deletes it.
  - Deleting it silences every shared deployment's reconciler: programs and breaks stay unmarked. Channels still air.
  - Delete it only once every deployment in the account and region is `per_stage`.
  - A `shared` deployment in an account without a router deploys without error but reconciles nothing, so check for
    `FastChannels-AsRun` before choosing `shared`.
- **Cost and latency.** One more Lambda invocation per as-run batch and about $1 per million EventBridge events.
  Reconciliation stays near real time (seconds).
- The reconciler takes either source (a log subscription batch or an EventBridge event), so a deployment can switch
  between `per_stage` and `shared` by redeploying its Media stack.
- The live stage keeps `per_stage` and its own filter.
