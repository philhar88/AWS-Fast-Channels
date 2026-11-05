# 0037 — Ad load by viewer tier: a cap and a bar per tier, applied in `/select`

**Status:** Proposed. It becomes Accepted once the end-to-end run on a fresh stage has shown two viewers of
different tiers in one break getting their tiers' pods.

## Context

A FAST channel gives every viewer the same break. DESIGN §16 values a retained viewer-hour and prices each extra ad
by λ, the viewer-retention cost of one more insertion. A high-value viewer who tunes away from ads should therefore
see fewer ads, and only well-paid ones; a tolerant viewer, or one whose breaks earn little, keeps the full pod.
`/select` was built with that hook ([0036](0036-pds-sidecar-in-the-prebid-server-task.md) and `core.adload`): a sold
ad must beat λ, and a house ad fills only where λ is 0. Until now λ was 0 for every slot.

DESIGN §16.1's full rule learns λ per viewer from a hazard model, a fatigue curve and a daily dual on the revenue
it may forgo. None of those inputs exists yet: the model needs months of logged breaks. The demo needs the behaviour
now, with a rule an operator can read.

Two more constraints apply:

- **Wire invariance** (DESIGN §16.1). A history-derived per-viewer floor is a value segment, which needs FULL consent
  to share, and a raised floor hides the bids below it, which ends forgone-revenue accounting. So the tier and the bar
  must never travel on the ticket, the bid request, MediaTailor's session state or a header.
- **`/select` carries no ticket.** Only `/policy` resolves the binding, and with it the viewer id. Both hooks of one
  auction reach the same task's sidecar ([0036](0036-pds-sidecar-in-the-prebid-server-task.md)).

## Decision

- **A profile per viewer.** The PDS table's item `VIEWER#<vid>` / `PROFILE` holds the viewer's persona and ad-load
  tier, keyed by the viewer id the binding resolves (a keyed hash of the identity provider's subject), never by the
  subject. In the demo the seed script writes it from `contracts/personas.json`; a learned model would write the same
  item. Only the PDS reads it.
- **A policy per tier** (`modules.pds.ad_load.<tier>`): `max_ads`, the most ads one break may hold (sold or house), and
  `bar_cpm`, the accepted CPM a sold ad must beat. λ is `bar_cpm / 1000 − c_ins` for every slot, so a sold ad runs
  exactly when `price × accept_ratio > bar_cpm`. λ stays at most λ_cap ($20 CPM-equivalent), so `bar_cpm` is at most
  $20.50. The default is the demo's `light` tier: one ad per break, above $20.
- **Who follows a tier.** FULL and PSEUDONYMOUS breaks with a bound viewer. KIDS, SAFE and CONTEXTUAL breaks, probes,
  breaks served without the `/policy` patch, viewers without a profile, and the tiers `standard` and `holdout` keep the
  control plan: every slot, at the market floor.
- **Tier-0 lines are held to the bar.** Where λ is 0 they run at any price, as before. Where a tier prices ad load,
  a guaranteed line must beat λ like any sold ad: λ is what one more ad costs the viewer, whoever pays for it, and a
  guaranteed line's delivery is over its flight, on the other tiers. The cap counts every ad, house ads included.
- **Minimum load.** A tier never empties a pod. When its rules leave nothing, the pod is the control plan's, with at
  most one ad, and the record says so (`ad_load.reason`: `min_load`).
- **The path.** `/policy` notes a FULL or PSEUDONYMOUS break's bound viewer under the break's `decision_id` and starts
  reading their tier on a reader thread while the auction runs. `/select` takes the viewer back and waits at most 20 ms
  for the tier. A tier is kept 5 minutes per viewer. A failed or late read gives no tier, which is the control plan.
  Nothing changes on the wire, in the module or in the contract.
- **Measurement.** The `pds.select` record holds the viewer's `tier` and the `ad_load` applied (policy, reason,
  `max_ads`, `bar_cpm`, slots), and every bid keeps its outcome: `below_bar` under the bar, `ad_load` beyond the cap.
  The lake view `v_ad_load_kpis` shows ads per break, the winning and turned-away prices, and revenue by tier.

## Consequences

- The demo shows the behaviour the design argues for: in one break, Dana (`light`) gets one $35 ad and slate while
  Ben (the holdout) gets the full pod. Bid requests are the same for both.
- The tier is assigned, not learned. It is a premise of the demo, labelled as such; the learned λ of DESIGN §16.1 would
  replace the per-tier bar without changing the hook, the profile item or the record.
- Revenue is turned away on purpose. The view's `turned_away_usd` bounds it from above (several turned-away bids may
  have competed for one slot), and the holdout tier is the counterfactual.
- A guaranteed line can deliver less on `light` viewers than a flat rule would. Its pacing must not assume every break.
- One more per-viewer read on the serving path. It runs off the request path, once per viewer per 5 minutes, and a
  failure falls back to the control plan, never to an empty pod or a fallback.

## Alternatives considered

- **A per-tier floor on the wire** (`imp.bidfloor`). Rejected by wire invariance: it would reveal the tier to every
  bidder and hide the bids below it.
- **A per-tier stored request** (fewer imps for `light`). Rejected for the same reason, and because the stored
  requests are per route, not per viewer.
- **The tier on the ticket row**, copied there at mint. Rejected: tiers stay inside the PDS (DESIGN §16.1), and a
  profile change would wait for the next ticket.
- **Exempting tier-0 lines from the bar.** On the demo's prices the $8 guaranteed line would take a `light` viewer's
  only slot, the opposite of fewer, higher-priced ads.
