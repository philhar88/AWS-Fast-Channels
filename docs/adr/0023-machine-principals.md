# 0023 — Machine principals: the Release Controller promotes and rolls back unattended

**Status:** Accepted

## Context

A release is promoted gradually over 30 minutes to 4 hours, and an alarm on a candidate sibling must roll it back
within seconds (P0-08: the pointer's monitor rolls back 0.4–2.8 s after the alarm; a remap reaches live sessions in
about 3 s). Waiting for a human would leave a bad release on air for as long as it takes someone to answer a page.

## Decision

- The Release Controller's roles form a machine principal (`release-controller`): it promotes and rolls back inside
  the release rules without a human approval. Every ledger record it writes names that principal
  (`release.accept`, `release.apply`, `release.point`, `release.promote`, `release.rollback`, `release.hold`,
  `release.fail`).
- Humans are notified of every promotion and rollback through the stage's alert topic; they never approve one.
- A rollback is never denied: no Budgets action or envelope rule applies to it. The ownership guard exempts only the
  controller's apply, promotion and reaper functions.
- Agents get machine principals of their own in Phase 3; until then the Release Controller is the only one.

## Consequences

- The ledger, not an approval queue, is the audit trail of releases.
- An operator who must stop a release in flight stops its execution (the pointer deployment then rolls back) or
  starts runbook R1.
