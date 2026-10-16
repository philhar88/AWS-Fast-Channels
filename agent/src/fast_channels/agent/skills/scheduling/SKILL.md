---
name: scheduling
description: How to build channel schedules with the schedule tools - read the brief, pick content, plan, review violations, get approval, publish, and extend a channel to a horizon. Use for any request to fill, extend, change or explain a channel schedule.
---
# Scheduling

## The loop: plan → review → approval → publish
1. **Know the channel**: `channels___get_channel` (ladder version, state, outputs) and `schedule___get_brief`
   (ratings, genres, dayparts, break policy). The brief is the operator's standing instruction; follow it
   unless the operator overrides it in this conversation.
2. **Know the schedule**: `schedule___get_schedule` returns the upcoming programs, the gaps between them and
   `end`, where the published schedule ends.
3. **Pick content**: `library___find_assets` with `kind=content`, `status=READY` and the channel's
   `ladder_version`, plus `catalog___search_content` / `catalog___get_content` for titles, ratings, genres and
   durations. Only assets encoded with the channel's exact ladder version can air on it. Respect the brief's
   ratings, genres and dayparts, and do not repeat a title within a block unless asked.
4. **Plan**: `schedule___plan` with the channel and the ordered `picks` (asset ids). Omit `start` and `after`
   to append at the end of the schedule, pass `start` (UTC ISO-8601) for an explicit start, or `after` with a
   program name to follow that program. Pass `break_policy` only to override the brief for this block. The
   tool computes every start time, on-air duration, ad break and SCTE-35 id and returns a `plan_id`, the
   program table, the `violations` and `publishable`. Never recompute or adjust its numbers.
5. **Review**: show the table (start UTC, title, asset, on-air duration, breaks) and every violation.
   Error-severity violations block publishing: change the picks or the start and plan again. Warnings (short
   lead time, filler before the block) are for the operator to accept.
6. **Publish**: `schedule___publish` with the `plan_id` only, while the plan is publishable and less than an
   hour old. The call itself asks the operator for approval: say in one sentence what will go on air, then call
   it (do not ask in text). On `PLAN_STALE` the schedule changed underneath you: read it again and plan again.
   Report any SCHEDULING_ERROR in the returned alerts.

## Filling a window
- Pick enough content to cover the window, then compare the plan's `end` with the end of the window: on an
  overrun drop or swap a pick, on a short fill add one, and plan again. Prefer a slightly short block over an
  overrun into the next daypart.
- On-air durations include the ad breaks, so a block needs less content than its length.
- To extend a channel to a horizon (for example 24 hours ahead), plan from the end of the published schedule
  in blocks that follow the brief's dayparts (at most 100 picks per plan), and publish each plan before
  planning the next.

## Changing a schedule
- `schedule___remove_programs` (approval) removes programs that have not started; later programs of the same
  block move up, and filler airs before the next block.
- Programs that are on air or have aired cannot be changed.

## Briefs
- `schedule___update_brief` (approval) replaces the whole brief. Start from `schedule___get_brief`, change only
  what the operator asked for, confirm the exact change, and restate the new brief afterwards.
