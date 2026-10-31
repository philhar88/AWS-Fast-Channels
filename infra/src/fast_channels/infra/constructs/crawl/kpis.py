"""``v_crawl_kpis``: the content-enrichment KPIs by hour, playback configuration and arm (DESIGN §9.8).

A lake view over the PDS's decision records, which the Pds stack adds with ``modules.crawl`` on:

- ``pds_policy`` gives each break (``decision_id`` = ``<sid>.<evid>``, probes left out) its arm
  (``contributed.enr``) and where its metadata was found (``contributed.ctx``);
- ``pds_select`` gives the auction: bids, the avail and the filled duration;
- ``pds_delivery`` gives our tracker's impressions, each priced at the bid whose creative it is (a pod's or a
  standalone's; house ads, which no bidder bid, at 0).

Coverage is the share of breaks with a UPID whose metadata was found; on the enriched arm it is the acceptance's
≥99 %. Repeated records of one break count once. Prices are the bidders' CPMs; with the demo's bidder simulator they
are simulated, so any difference between the arms is too.
"""

from typing import Final

from fast_channels.core.crawl.names import CONTRIBUTOR, KPI_VIEW, RECORD_ARM, RECORD_SOURCE, Source
from fast_channels.core.pds.names import decision_table
from fast_channels.core.pds.pod import Outcome
from fast_channels.core.pds.records import DELIVERY_RECORD, POLICY_RECORD, SELECT_RECORD
from fast_channels.infra.constructs.ops.lake import View

_FOUND: Final = f"('{Source.SNAPSHOT.value}', '{Source.HEADER.value}')"
_RENDERED: Final = f"('{Outcome.WON.value}', '{Outcome.STANDALONE.value}')"
"""Outcomes of the bids whose ads the pod renders: the price of an impression of their creative."""

KPIS: Final = View(
    KPI_VIEW,
    "Content enrichment by hour, playback configuration and arm (enr 1 enriched, 0 holdout): coverage, bids, fill, "
    "impressions and revenue",
    (
        ("datehour", "varchar"),
        ("configuration", "varchar"),
        ("enr", "bigint"),
        ("breaks", "bigint"),
        ("with_upid", "bigint"),
        ("covered", "bigint"),
        ("from_header", "bigint"),
        ("coverage", "double"),
        ("auctions", "bigint"),
        ("bids", "bigint"),
        ("bid_rate", "double"),
        ("avail_seconds", "double"),
        ("filled_seconds", "double"),
        ("fill_rate", "double"),
        ("filled_breaks", "bigint"),
        ("impressions", "bigint"),
        ("revenue_usd", "double"),
        ("ecpm", "double"),
        ("revenue_per_1000_breaks", "double"),
    ),
    f"""
WITH policy AS (
  SELECT decision_id, min(datehour) AS datehour, arbitrary(pc) AS configuration, max(upid) AS upid,
    max(TRY_CAST(json_extract_scalar(contributed, '$.{RECORD_ARM}') AS bigint)) AS enr,
    max(json_extract_scalar(contributed, '$.{RECORD_SOURCE}')) AS ctx
  FROM "{{db}}"."{decision_table("policy")}"
  WHERE record = '{POLICY_RECORD}' AND NOT probe AND contains(contributors, '{CONTRIBUTOR}')
  GROUP BY decision_id
),
auctions AS (
  SELECT decision_id, max(cardinality(bids)) AS bids, max(dur_ms) AS dur_ms, max(filled_ms) AS filled_ms
  FROM "{{db}}"."{decision_table("select")}" WHERE record = '{SELECT_RECORD}'
  GROUP BY decision_id
),
won AS (
  SELECT DISTINCT s.decision_id, json_extract_scalar(b, '$.crid') AS crid,
    TRY_CAST(json_extract_scalar(b, '$.price') AS double) AS cpm
  FROM "{{db}}"."{decision_table("select")}" s CROSS JOIN UNNEST(s.bids) AS t (b)
  WHERE s.record = '{SELECT_RECORD}' AND json_extract_scalar(b, '$.outcome') IN {_RENDERED}
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
SELECT p.datehour, p.configuration, p.enr, count(*) AS breaks,
  count_if(p.upid <> '') AS with_upid,
  count_if(p.ctx IN {_FOUND}) AS covered,
  count_if(p.ctx = '{Source.HEADER.value}') AS from_header,
  CAST(count_if(p.ctx IN {_FOUND}) AS double) / nullif(count_if(p.upid <> ''), 0) AS coverage,
  count(a.decision_id) AS auctions, sum(a.bids) AS bids,
  CAST(sum(a.bids) AS double) / nullif(count(a.decision_id), 0) AS bid_rate,
  sum(a.dur_ms) / 1000e0 AS avail_seconds, sum(a.filled_ms) / 1000e0 AS filled_seconds,
  CAST(sum(a.filled_ms) AS double) / nullif(sum(a.dur_ms), 0) AS fill_rate,
  count(e.decision_id) AS filled_breaks, sum(e.impressions) AS impressions, sum(e.revenue_usd) AS revenue_usd,
  sum(e.revenue_usd) * 1000 / nullif(sum(e.impressions), 0) AS ecpm,
  sum(e.revenue_usd) * 1000 / nullif(count(*), 0) AS revenue_per_1000_breaks
FROM policy p
LEFT JOIN auctions a ON a.decision_id = p.decision_id
LEFT JOIN earned e ON e.decision_id = p.decision_id
GROUP BY p.datehour, p.configuration, p.enr
""",  # noqa: S608 - constant SQL, nothing from input
)
"""The view (module docstring)."""
