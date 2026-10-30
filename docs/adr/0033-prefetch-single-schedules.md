# 0033 — Ad prefetch with rolling SINGLE schedules from the schedule of record

**Status:** Accepted

## Context

A FAST break is a herd. Every session of a configuration reaches the same SCTE-35 marker within one playlist poll,
so every session's ad request arrives at Prebid Server and the Pod Decision Service in the same few seconds. Sized
for that peak, the PDS needed about 0.34 provisioned Lambda environments per session in a break herd (lane D's
M2-x2 measurements; the 2026-10-30 decision moved the PDS into the Prebid Server task as a sidecar). A creative that
MediaTailor has not prepared before is also skipped at its first sight (`NEW_CREATIVE`, `IMPORT_IN_PROGRESS`,
`TRANSCODE_IN_PROGRESS`), so a break full of new creatives airs slate.

MediaTailor ad prefetch retrieves a break's ads before the break, for each session that requests a playlist in a
retrieval window, and can spread those requests over a time window (`RETRIEVAL_WINDOW` traffic shaping). Phase 0
(P0-02, October 2026) measured it on Channel Assembly:

- A **SINGLE** schedule per break, matched on `scte.event_id`, filled every target break from its retrieval, for
  every session that retrieved. Non-target breaks, including one inside another schedule's consumption window, kept
  real-time insertion.
- **First sight disappears.** Never-seen preconditioned and MP4 creatives filled at their first break when
  prefetched; the MP4 transcode ran at retrieval (19 s). The same pods without prefetch lost both creatives at every
  break.
- **Hooks fire on retrieval.** PRE_ADS (our content lookup) and POST_ADS run for every prefetch request.
- **What a retrieval carries.** `session.uuid`, `session.user_agent` and `player_params.*` are substituted
  (contrary to the user guide); `scte.*` values come only from the schedule's `DynamicVariables`, and
  `session.avail_duration_ms` is rounded to whole seconds (`60060` reaches the ADS as `60000`).
- A **RECURRING** schedule also works, but carries no event id or UPID and a 300 s duration, so the PDS could not
  match the break and Prebid Server would look for a 300 s stored request.
- `ListPrefetchSchedules` hides RECURRING schedules unless `ScheduleType=ALL`; `adBreakCorrelationId` is missing on
  prefetch events, and a prefetch-consumed `FILLED_AVAIL` carries the session's previous request URL.
- Retrieval transcodes bill whether or not the break airs; MediaTailor allows 25 active schedules per configuration.

## Decision

- **The Release Controller owns prefetch.** Its prefetcher (`fast_channels.functions.release.prefetch`, with
  `core.monetization.prefetch`) runs every 5 minutes, on `Schedule Published` and on `Census Changed`. For each
  monetized channel that is running, it reads the schedule of record (`GetChannelSchedule`, the next 6 hours, and
  `DescribeProgram` for the event id and MPU UPID of each break) and keeps every prefetching sibling
  (`modules.release.prefetch.siblings`: the general pair `a`/`b` and the kids pair `k`/`l`; never the probe) scheduled
  for its next `horizon_breaks` breaks (20; at most 24, under the quota).
- **Rolling, create-only SINGLE schedules,** one per break and sibling, named `pf-<event id>` and tagged as the
  controller's. A schedule is never updated: the schedule of a break that moved is deleted and created again, and the
  schedule of a break that left the schedule (a re-plan) is deleted, so no retrieval transcode is paid for a break
  that will not air. A sibling with a RECURRING schedule gets no SINGLE one (never mixed); the prefetcher raises its
  alarm instead. The schedules live outside CloudFormation; the release reaper deletes them before the siblings.
- **The schedule** (`schedule_request`):
  - retrieval from 150 s to 30 s before the break, with `RETRIEVAL_WINDOW` traffic shaping over 90 s;
  - consumption from 20 s before the break to 10 s after it, matched on `scte.event_id`;
  - `DynamicVariables`: rule R-S1's `scte.event_id`, `scte.segmentation_upid` (the content id, as MediaTailor passes
    it in real time) and `session.avail_duration_ms`, plus `session.avail_duration_secs`, from which the POST body
    names its stored request.
  Every request passes validator R-S1 before it is sent. A break without an MPU UPID, or whose length is not one of
  the stage's break lengths, keeps real-time insertion.
- **The ad path on retrieval is the real-time one.** The PDS answers about 150 s ahead: its rule for the rounded
  duration checks the pod against the break of record, a request whose binding cannot be resolved is CONTEXTUAL as
  always, and reservations are taken at retrieval. Delivery joins use our tracker (`d=<sid>.<evid>`), never
  `adBreakCorrelationId` or `FILLED_AVAIL.adsRequestUrl`. A reservation whose break the session never watched is
  released by the sweeper after its 2 hours.
- **The schedule of record decides (the owner's direction, 2026-10-30).** "Because the channel assembly schedule is
  somewhat deterministic you should be able to do elegant prefetch schedule mostly aligned with that. The few late
  joiners getting realtime is ok. People joining during a break also being realtime partial render of ads is ok."
  - Every upcoming break's schedule derives from `GetChannelSchedule` alone, and a run over an unchanged schedule
    writes nothing.
  - A break has moved only when its start is more than 3 s (half a 6.006 s segment) from its schedule's, both in
    whole seconds (MediaTailor keeps schedule times in whole seconds, P0-02). A smaller shift is Channel Assembly's
    approximate start moving between reads, and keeps the schedule untouched, retrieved ads included.
  - Lane D's M8-08 run 5 (2026-10-30) showed the cost of chasing a shift. A 2 s exact comparison deleted a schedule
    every session had retrieved, after a 1.2 s shift. Every session then made a second auction at the break, with
    double reservations.
  - Late joiners and viewers who join during a break get real-time insertion (a partial render for the latter). That
    is accepted behaviour: nothing is built to avoid it.
- **No YO on prefetch configurations:** a sibling's model cannot carry `YieldOptimizationConfiguration`.
- `modules.release.prefetch.enabled` (on by default with the module) turns it off; the timings are settings.

## Evidence

`tests/e2e/test_16_prefetch.py` on a throwaway stage, 2026-10-30: 8 breaks of 60.06 s, 40 sessions on each general
sibling, Prebid Server 4.4.0.

- Real-time ad requests arrived within 4.1 s (p05–p95) after the marker; unshaped retrievals within 3.4–3.9 s after
  the retrieval opened (150 s ahead); shaped ones (90 s window) from 150 s to 67 s ahead, p05–p95 59–72 s.
- 600 of 600 prefetched avails filled, none losing a creative to first sight, including the first break, where
  MediaTailor first saw the demand's creatives through a retrieval.
- A real-time first sight on the same stage (its v3.0.0 configuration's house ads) skipped both creatives.
- Retrievals carried `dur=60000`, `evid` and `upid`. The PDS recorded the session id, event id and UPID of each, and
  the PRE_ADS content lookup ran at retrieval.
- Line ads reserved at retrieval were delivered by our tracker.

## Consequences

- The ad request herd moves off the break and spreads over the shaping window, so the PDS and Prebid Server see a
  ramp of about (sessions ÷ 90) requests per second per sibling instead of a peak at the marker.
- New creatives are prepared before they air: prefetched breaks lose no ad to first sight.
- Retrieval transcodes cost money for breaks that do not air, bounded by the horizon and by deleting the schedules of
  re-planned breaks. A sibling with no sessions (an idle sibling outside smoke tests) retrieves nothing.
- A session that joins after a break's retrieval window gets real-time insertion for that break, with its first-sight
  risk; the per-slot standalones (ad buffet) cover it.
- The controller writes up to 24 schedules per sibling and channel; the ownership guard exempts the prefetcher.
- A RECURRING fallback for breaks the schedule of record cannot describe is not built.
- See [release-controller.md](../release-controller.md#prefetch).
