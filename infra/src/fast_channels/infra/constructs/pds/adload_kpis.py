"""``v_ad_load_kpis``: the ad load by hour, playback configuration, viewer tier and the policy applied (ADR-0037).

A lake view over the PDS's decision records, which the Pds stack adds:

- ``pds_select`` gives each break (``decision_id``, probes left out) the viewer's tier (``tier``, ``none`` without
  one), the policy applied and why (``ad_load.policy``, ``ad_load.reason``), the slots offered, the pod and its
  filled duration, and every bid at or above the floor with its outcome;
- ``pds_delivery`` gives our tracker's impressions, each priced at the bid whose creative it is (a pod's or a
  standalone's; house ads, which no bidder bid, at 0). An ad counts once, whichever delivery source saw it first.

``turned_away_usd`` adds up every sold bid the tier turned away (under its bar, or beyond its ``max_ads``) at its
price: an upper bound on the revenue forgone, since several of them may have competed for one slot. Prices are the
bidders' CPMs; with the demo's bidder simulator they are simulated.
"""

from typing import Final

from fast_channels.core.pds.names import AD_LOAD_VIEW, decision_table
from fast_channels.core.pds.pod import Outcome
from fast_channels.core.pds.records import DELIVERY_RECORD, SELECT_RECORD
from fast_channels.infra.constructs.ops.lake import View

_RENDERED: Final = f"('{Outcome.WON.value}', '{Outcome.STANDALONE.value}')"
"""Outcomes of the bids whose ads the pod renders: the price of an impression of their creative."""
_TURNED_AWAY: Final = f"('{Outcome.BAR.value}', '{Outcome.AD_LOAD.value}')"
"""Outcomes of the sold bids the tier turned away."""

AD_LOAD_KPIS: Final = View(
    AD_LOAD_VIEW,
    "Ad load by hour, playback configuration, viewer tier and the policy applied: ads per break, fill, the winning "
    "and turned-away prices, impressions and revenue",
    (
        ("datehour", "varchar"),
        ("configuration", "varchar"),
        ("tier", "varchar"),
        ("policy", "varchar"),
        ("reason", "varchar"),
        ("breaks", "bigint"),
        ("slots", "bigint"),
        ("ads", "bigint"),
        ("ads_per_break", "double"),
        ("sold_ads", "bigint"),
        ("avail_seconds", "double"),
        ("filled_seconds", "double"),
        ("fill_rate", "double"),
        ("winning_cpm_avg", "double"),
        ("winning_cpm_min", "double"),
        ("turned_away_bids", "bigint"),
        ("turned_away_usd", "double"),
        ("impressions", "bigint"),
        ("revenue_usd", "double"),
        ("ecpm", "double"),
    ),
    f"""
WITH breaks AS (
  SELECT decision_id, min(datehour) AS datehour, arbitrary(pc) AS configuration,
    coalesce(arbitrary(tier), 'none') AS tier,
    coalesce(arbitrary(json_extract_scalar(ad_load, '$.policy')), 'standard') AS policy,
    coalesce(arbitrary(json_extract_scalar(ad_load, '$.reason')), 'control') AS reason,
    max(TRY_CAST(json_extract_scalar(ad_load, '$.slots') AS bigint)) AS slots,
    max(cardinality(pod)) AS ads, max(dur_ms) AS dur_ms, max(filled_ms) AS filled_ms
  FROM "{{db}}"."{decision_table("select")}"
  WHERE record = '{SELECT_RECORD}' AND NOT probe
  GROUP BY decision_id
),
bids AS (
  SELECT DISTINCT s.decision_id, json_extract_scalar(b, '$.ref') AS ref, json_extract_scalar(b, '$.crid') AS crid,
    json_extract_scalar(b, '$.outcome') AS outcome,
    coalesce(TRY_CAST(json_extract_scalar(b, '$.house') AS boolean), false) AS house,
    TRY_CAST(json_extract_scalar(b, '$.price') AS double) AS cpm
  FROM "{{db}}"."{decision_table("select")}" s CROSS JOIN UNNEST(s.bids) AS t (b)
  WHERE s.record = '{SELECT_RECORD}' AND NOT s.probe
),
priced AS (
  SELECT decision_id,
    count_if(outcome = '{Outcome.WON.value}' AND NOT house) AS sold_ads,
    sum(cpm) FILTER (WHERE outcome = '{Outcome.WON.value}' AND NOT house) AS winning_cpm_sum,
    min(cpm) FILTER (WHERE outcome = '{Outcome.WON.value}' AND NOT house) AS winning_cpm_min,
    count_if(outcome IN {_TURNED_AWAY} AND NOT house) AS turned_away_bids,
    coalesce(sum(cpm) FILTER (WHERE outcome IN {_TURNED_AWAY} AND NOT house), 0) / 1000 AS turned_away_usd
  FROM bids
  GROUP BY decision_id
),
won AS (
  SELECT DISTINCT decision_id, crid, CASE WHEN house THEN 0 ELSE cpm END AS cpm
  FROM bids WHERE outcome IN {_RENDERED}
),
earned AS (
  SELECT d.decision_id, count(*) AS impressions, sum(coalesce(w.cpm, 0)) / 1000 AS revenue_usd
  FROM (
    SELECT DISTINCT decision_id, crid FROM "{{db}}"."{decision_table("delivery")}"
    WHERE record = '{DELIVERY_RECORD}' AND impression
  ) d
  LEFT JOIN won w ON w.decision_id = d.decision_id AND w.crid = d.crid
  GROUP BY d.decision_id
)
SELECT b.datehour, b.configuration, b.tier, b.policy, b.reason, count(*) AS breaks,
  sum(b.slots) AS slots, sum(b.ads) AS ads, CAST(sum(b.ads) AS double) / count(*) AS ads_per_break,
  sum(p.sold_ads) AS sold_ads,
  sum(b.dur_ms) / 1000e0 AS avail_seconds, sum(b.filled_ms) / 1000e0 AS filled_seconds,
  CAST(sum(b.filled_ms) AS double) / nullif(sum(b.dur_ms), 0) AS fill_rate,
  sum(p.winning_cpm_sum) / nullif(sum(p.sold_ads), 0) AS winning_cpm_avg,
  min(p.winning_cpm_min) AS winning_cpm_min,
  sum(p.turned_away_bids) AS turned_away_bids, sum(p.turned_away_usd) AS turned_away_usd,
  sum(e.impressions) AS impressions, sum(e.revenue_usd) AS revenue_usd,
  sum(e.revenue_usd) * 1000 / nullif(sum(e.impressions), 0) AS ecpm
FROM breaks b
LEFT JOIN priced p ON p.decision_id = b.decision_id
LEFT JOIN earned e ON e.decision_id = b.decision_id
GROUP BY b.datehour, b.configuration, b.tier, b.policy, b.reason
""",  # noqa: S608 - constant SQL, nothing from input
)
"""The view (module docstring)."""
