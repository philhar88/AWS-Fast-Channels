---
name: scheduling
description: How to build channel schedules with the schedule tools - read the brief, pick content, plan, review violations, publish, and extend a channel to a horizon. Use for any request to fill, extend, change or explain a channel schedule.
---
# Scheduling

## The loop
1. **Know the channel**: `channels___get_channel` (ladder version, state) and `schedule___get_brief`
   (ratings, genres, dayparts, break policy). The brief is the operator's standing instruction; follow it
   unless the operator overrides it in this conversation.
2. **Know the schedule**: `schedule___get_schedule` returns the published programs and the gaps. Plan from the
   end of the last program (`after`) or from an explicit start.
3. **Pick content**: `library___find_assets` for assets that are READY on the channel's ladder version, and
   `catalog___search_content` / `catalog___get_content` for titles, ratings and genres. Only pick assets that
   exist on the channel's ladder; respect ratings, dayparts and genre rules from the brief; avoid repeating a
   title within a block unless asked.
4. **Plan**: `schedule___plan` with the channel, the start or `after`, the ordered picks and the break policy.
   The tool computes every start time, duration and break, and returns a `plan_id`, the program table and
   violations. Do not recompute or adjust its numbers.
5. **Review**: show the table and every violation. Errors block publishing: change the picks or the window
   and plan again. Warnings (for example short lead time) are for the operator to accept.
6. **Publish**: `schedule___publish` with the `plan_id` only. This needs approval. On `PLAN_STALE` the
   schedule changed underneath you: plan again from the fresh schedule.

## Filling a window
- Choose enough content to cover the window; the plan tool reports under-fill and overrun. Prefer a slightly
  short block over an overrun into the next daypart.
- To extend a channel to a horizon (for example 24 hours ahead), plan from the end of the published schedule
  in blocks that follow the brief's dayparts, then publish each plan.

## Briefs
- `schedule___update_brief` changes the standing rules (needs approval). Confirm the exact change with the
  operator first and restate the new brief afterwards.
