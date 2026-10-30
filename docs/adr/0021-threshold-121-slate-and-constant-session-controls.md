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
- The PRE_SESSION tree is constants only: `CUSTOM_OUTPUT` `ps_ctl` with `aws.availSuppressionMode` `AFTER_LIVE_EDGE`,
  `aws.availSuppressionValue` `00:00:00`, `aws.availSuppressionFillPolicy` `PARTIAL_AVAIL` (amended 2026-10-30,
  below; it was `BEHIND_LIVE_EDGE` `00:00:00`), `aws.reportingMode` `server`, `aws.logMode` `DISABLED`,
  `aws.preroll` `disabled`, re-exported by the sequential root. `AvailSuppression` on the configuration (the same
  mode, value and fill policy) is defence in depth.
- No `PRE_MANIFEST` mapping.
- The probe and lab siblings publish the PRE_SESSION hook's opt-in events, so the committed constants can be read in
  their logs; ticketed siblings never do (the events may carry the hook's output).

## Consequences

- An unfilled slot shows the break slate, not a shortened break.
- Changing a session control means a new release of the PRE_SESSION tree, with the strictest gate (smoke on two
  breaks and a soak), because a bad value persists for running sessions.

## Amendment, 2026-10-30: mid-break joiners get ads (M3-07)

**The owner's decision:** "mid break join should get ads inserted". This reverses `BEHIND_LIVE_EDGE`, which showed a
viewer who joined during a break the slate for the rest of it.

**The candidates** were each set by the PRE_SESSION constants and the configuration's `AvailSuppression` together:
- (a) `OFF`, value `00:00:00`;
- (b) `AFTER_LIVE_EDGE`, value `00:00:00`, with `PARTIAL_AVAIL`.

The user guide documents (b) as "partial ad break fills when a session starts mid-break": MediaTailor personalizes
the part of the break after the live edge and not the part before it.

**The measurement** (2026-10-30, throwaway rig `fastcie-exp-mbj`, the P0-25 way) used:
- a BASIC Channel Assembly channel, a 60 s manifest window and 120.12 s breaks;
- a VAST ADS of 30.03 s ads, threshold 121 and server-side reporting;
- joins about 30 s and about 76 s into a break, four each per candidate over two breaks, by players that fetch every
  segment, plus one player per candidate joining before each break.

| Join | (a) `OFF` | (b) `AFTER_LIVE_EDGE` + `PARTIAL_AVAIL` |
|---|---|---|
| +30 s: ADS request | `dur_ms` 120120 | `dur_ms` 90090 |
| +30 s: stitched | 4 ads, anchored at the break's `START-DATE` | about 30 s of break slate, then 3 ads |
| +30 s: viewer enters | ad 1 at its third segment (4 of 4 players) | every ad at its first segment |
| +30 s: server-side beacons | ad 1: midpoint, thirdQuartile, complete, no impression; ads 2–4 full | 3 ads, impression through complete |
| +76 s: ADS request | `dur_ms` 108120 or 114120 | `dur_ms` 48054 or 42054 |
| +76 s: stitched | a remainder pod of 3 ads from the window head | slate, then 1 ad |
| +76 s: viewer enters | ad 2 mid-way (4 of 4); ad 1 is stitched and never seen | the ad's first segment |
| +76 s: server-side beacons | ad 1 none; ad 2 quartiles without an impression; ad 3 full | 1 ad, impression through complete |
| Joined before the break | 4 of 4 ads, full | 4 of 4 ads, full |

With (a), each pre-break joiner also asked the ADS for the tail of the break that had just ended (18.12 s) and got
an empty response; (b) made no such request.

**The choice: (b), `AFTER_LIVE_EDGE` `00:00:00` with `PARTIAL_AVAIL`.**
- A viewer who joins mid-break gets ads for what is left of the break, and every inserted ad plays from its first
  segment.
- No ad is stitched unseen, and none is reported partly: its impression and every quartile fire together.
- With (a), no impression fired for an ad entered mid-way either. But that ad's quartiles fired, the viewer entered
  mid-ad, and the pod carried ads (and MediaTailor's insertion charge for them) that the viewer never saw from their
  start.

**The residual:**
- The viewer sees the break slate for the part of the break that aired before the join, and for what no 30 s ad fits
  at its end.
- A join in a break's last 30 s gets no ad.

**What follows:**
- **Remainder durations.** MediaTailor asks for the remainder at segment granularity: 90,090 ms at +30 s, and 48,054
  or 42,054 ms at +76 s, in the measurement.
  - On the Prebid Server route, `fast-pod-{{session.avail_duration_secs}}s` names a stored request for the grid
    durations only (30, 60, 90 and 120 s; R-P5). A remainder that rounds to one of them is filled; any other names
    none, and Prebid Server answers 400, so the slate plays, as it did for the whole break with `BEHIND_LIVE_EDGE`.
  - Remainders are filled once the stored request takes its pod length from `session.avail_duration_secs` instead of
    its name: one stored request per route, which replaces the per-length ones.
  - The Pod Decision Service's pod rules already take any pod duration (Σ dur ≤ poddur, Σ VAST durations ≤ the
    break).
- **The smoke test** still leaves out a break its session joined: its pod is a remainder, which the verdict does not
  judge.
