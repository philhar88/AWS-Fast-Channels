# 0021 — Personalization threshold 121, the break slate for unfilled time, constant session controls, no PRE_MANIFEST

**Status:** Accepted

## Context

A monetized sibling stitches pods that Prebid Server assembles per break (one 30 s slot per grid step) and plays
the rest of the break as Channel Assembly's slate. Phase 0 measured MediaTailor's choices (P0-07, P0-08, P0-20):

- With `PersonalizationThresholdSeconds` at 5, MediaTailor abandons a pod that leaves more than 5 s unfilled, so a
  partial pod from a thin auction airs nothing. At 121 (more than the longest break) it keeps every ad it got and
  the break slate plays the rest.
- A viewer who joins mid-break would otherwise see a partially filled avail with its timing broken; `aws.availSuppression`
  `BEHIND_LIVE_EDGE` with `00:00:00` makes mid-break joins see slate.
- Session-level controls set by a `PRE_SESSION_INITIALIZATION` function persist for the session: a remap cannot repair
  them, and a value a player can influence there could switch ads off for the session.
- `PRE_MANIFEST` runs on every manifest request: its cost and latency grow with the audience, not with the breaks.

## Decision

- `PersonalizationThresholdSeconds` = 121 on every sibling; `SlateAdUrl` unset (the break slate plays).
- The PRE_SESSION tree is constants only: `CUSTOM_OUTPUT` `ps_ctl` with `aws.availSuppressionMode` `BEHIND_LIVE_EDGE`,
  `aws.availSuppressionValue` `00:00:00`, `aws.reportingMode` `server`, `aws.logMode` `DISABLED`, `aws.preroll`
  `disabled`, re-exported by the sequential root. `AvailSuppression` on the configuration is defence in depth.
- No `PRE_MANIFEST` mapping.
- The probe and lab siblings publish the PRE_SESSION hook's opt-in events, so the committed constants can be read in
  their logs; ticketed siblings never do (the events may carry the hook's output).

## Consequences

- An unfilled slot shows the break slate, not a shortened break.
- Changing a session control means a new release of the PRE_SESSION tree, with the strictest gate (smoke on two
  breaks and a soak), because a bad value persists for running sessions.
