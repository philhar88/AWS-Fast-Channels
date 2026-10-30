# 0033 — Every Function request is cached, keyed by the scope its answer is valid for

**Status:** Accepted

## Context

A MediaTailor Monetization Function of type `HTTP_REQUEST` may set `Cache`: `TtlMinimumSeconds` and
`TtlMaximumSeconds` (1–300 s) and a `Key` expression. The user guide describes caching only through `Cache`, and it
documents no cache for a request that sets none.

During the release module's smoke tests (October 2026), MediaTailor answered such a request from a cache anyway:

- A PRE_ADS `HTTP_REQUEST` with a literal URL and no `Cache` called its upstream once, at the first break. For the
  next 40 minutes and six breaks it called it no more, also under releases with new FunctionIds. The hook reported
  success and its latency stayed at 43–60 ms after the upstream was made to answer in 2,500 ms, beyond the request's
  1,000 ms timeout.
- A URL that varied per break, in its query or its path, gave one or two upstream calls, then none for later breaks
  with other event ids. So the implicit key does not seem to contain the evaluated URL.
- The same request with an explicit `Cache` keyed on the break's event id (1 s) called the upstream once per break.

Without an explicit key, the answer of one session or break can be served to another. That breaks requests whose
answers are per viewer or per break: a ticket redeem (one session's binding returned to another) or an auction (one
session's bids served to every session). The behaviour is reported to AWS as a documentation gap or defect.

Validator R-F4 (DESIGN §7) forbade `session.*` in keys, because the cache is shared by every session of a playback
configuration and split into 4–5 partitions, so a per-viewer key hits rarely. That reasoning holds for hit rates, but
for per-session answers the key is what keeps them apart, not what makes them hit.

## Decision

Every `HTTP_REQUEST` in the release bundle sets `Cache`. Its `Cache` declares a scope and gives the reason for that
scope in words. Those two members stay in the bundle and are left out of `PutFunction`. The key names exactly the
scope's identifiers, and the TTL fits how long one answer stays right:

| Scope | One answer per | The key names | `TtlMaximumSeconds` at most |
|---|---|---|---|
| `title` | title | the break's UPID (`scte.segmentation_upid`; the content id) | 300 s |
| `break` | break of a channel | the channel (a `\|<channel>\|` segment) and `scte.event_id` or `scte.segmentation_event_id` | the shortest break the stage airs (30 s) |
| `session` | viewing session | `session.uuid` | 300 s |
| `session_break` | session and break (an auction) | `session.uuid` and the break's event id | the shortest break (30 s) |
| `global` | playback configuration | nothing beyond the key's prefix (only with a written reason) | 300 s |

Validator R-F8 enforces it, beside the other R-F rules:

- a request with no `Cache`, a scope or a reason missing, or a reason that is not a sentence, is refused;
- a key must read one value of every identifier group of its scope and no other input, so a key cannot claim a wider
  scope than it varies by;
- a `break` key must name its sibling's channel, because SCTE-35 event ids are unique per channel only. A
  deployment-wide function therefore cannot serve the breaks of two channels;
- the TTL may not outlive the scope: the break scopes take the bundle's shortest grid duration, every TTL is at least
  1 s, and none exceeds the cache's 300 s.

R-F4 changes with it. A key still starts with `'<name>|v<N>|'` and never reads `player_params.*` (a viewer could
choose another viewer's entry) or `avail.random`. It may read a session only as `session.uuid`, and R-F8 allows that
only in the `session` scopes.

The crawl lookup (M17-03) is `title`, as it was keyed before. The release module's end-to-end lookup is a `break`
key with the channel. The PRE_ADS auction of the ad-server chain (M8-09) is `session_break`.

## Consequences

- No request in the bundle can fall back on the undocumented default cache. A request that would need no caching
  still gets a key that makes every answer unique, and at worst it caches nothing useful.
- Per-session keys cost memory and hit only on a session's own repeat calls. The 4–5 cache partitions make even those
  hits uncertain. A session-scoped request therefore still has to tolerate one upstream call per call it makes.
- A `break` request is per sibling, not per deployment, because its key names the channel: more FunctionIds per
  release (the 20-per-hook execution limit is unaffected).
- The rule does not repair the default cache. A request mapped outside the bundle (a hand-made configuration) can
  still be served from it, so the finding stays open with AWS.
- Evidence: the release module's smoke runs (the upstream's own invocation log against
  `PreAdsRequestHook.Invocations`), and the M3-x2 live check of a `session_break` key on a throwaway stage: one
  upstream call per session and break.
