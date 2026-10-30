# 0034 — Every Function request declares how its answers may be reused, and is cached by that scope

**Status:** Accepted

## Context

A MediaTailor Monetization Function of type `HTTP_REQUEST` may set `Cache`: `TtlMinimumSeconds` and
`TtlMaximumSeconds` (1–300 s) and a `Key` expression. Cached answers are shared by every session of a playback
configuration and split into 4–5 partitions (the crawl lookup measured 4 with herds of 100 and 300 sessions and 5 with
herds of 12 and 40). Without `Cache`, every call goes to the upstream.

Every Function call spends time and upstream capacity: part of the 2,000 ms that the request-path hooks share and of
the Live maximum that also covers the ad request, and, at a break herd, one upstream call per session. So the owner
decided that every request is cached wherever its answer can be reused, and that its key holds exactly the values
that make an answer unique. A
key with too few values serves one session's or one break's answer to another: an auction keyed on the break alone
gives every viewer the first viewer's bids. A key with values the answer does not depend on only spends cache memory
and upstream calls.

Validator R-F4 forbade `session.*` in keys, because per-viewer keys hit rarely across the partitions. That
holds for hit rates, but for per-session answers the key is what keeps them apart, not what makes them hit.

## Decision

Every `HTTP_REQUEST` in the release bundle declares a cache scope and gives the reason for it in words. Those two
members stay in the bundle and are left out of `PutFunction`. Anything cacheable is cached: the request sets `Cache`,
its key names exactly the scope's identifiers, and the TTL fits how long one answer stays right.

| Scope | One answer per | The key names | `TtlMaximumSeconds` at most |
|---|---|---|---|
| `title` | title | the break's UPID (`scte.segmentation_upid`; the content id) | 300 s |
| `break` | break of a channel | the channel (a `\|<channel>\|` segment) and `scte.event_id` or `scte.segmentation_event_id` | the shortest break the stage airs (30 s) |
| `session` | viewing session | `session.uuid` | 300 s |
| `session_break` | session and break (an auction) | `session.uuid` and the break's event id | the shortest break (30 s) |
| `global` | playback configuration | nothing beyond the key's prefix | 300 s |
| `none` | call (never cached) | no `Cache` | — |

`none` is for a single-use call that must reach its service every time, such as M4's ticket redeem.

Validator R-F8 enforces it, beside the other R-F rules:

- a request with no scope, or a reason that is not a sentence, is refused;
- a scope other than `none` without `Cache` is refused, and `none` with `Cache` is refused;
- a key must read one value of every identifier group of its scope and no other input, so a key cannot claim a wider
  scope than it varies by;
- a `break` key must name its sibling's channel, because SCTE-35 event ids are unique per channel only. A
  deployment-wide function therefore cannot serve the breaks of two channels;
- the TTL may not outlive the scope: the break scopes take the bundle's shortest grid duration, every TTL is at least
  1 s, and none exceeds the cache's 300 s.

R-F4 changes with it. A key still starts with `'<name>|v<N>|'` and never reads `player_params.*` (a viewer could
choose another viewer's entry) or `avail.random`. It may read a session only as `session.uuid`, and R-F8 allows that
only in the `session` scopes.

The crawl lookup is `title`, as it was keyed before. The release module's end-to-end lookup is a `break`
key with the channel. The PRE_ADS auction of the ad-server chain is `session_break`.

## Consequences

- Every answer's reuse is explicit and checked before anything is put. A request that should never be reused says
  so (`none`) and why.
- Per-session keys cost memory and hit only on a session's own repeat calls, and the 4–5 partitions make even those
  hits uncertain. A session-scoped request still has to tolerate one upstream call per call it makes.
- A `break` request is per sibling, not per deployment, because its key names the channel: more FunctionIds per
  release (the 20-per-hook execution limit is unaffected).
- A request mapped outside the bundle (a hand-made configuration) is not covered.
- Evidence, from the upstream's own invocation log matched to the ADS's request log on a throwaway stage (6 sessions,
  7–8 breaks per configuration):
  - a `session_break` key: one upstream call for each of 39, 48 and 42 (session, break) pairs, and no answer
    reached another session or break;
  - a `break` key: 31 calls for 39 session-breaks, up to 5–6 per break for 6 sessions (the cache's partitions);
  - no `Cache`: one call per ADS request.
